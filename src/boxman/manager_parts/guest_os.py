"""
A VM's guest OS (``os:``), and what ``os: windows`` changes.

Every VM is ``os: linux`` unless it says otherwise, and nothing about a
Linux VM changes here. An ``os: windows`` VM differs in five ways
(doc/windows-guests.md, §5.2):

- its ssh user is ``Administrator``, or its ``admin_user``;
- boxman's public key reaches it through its answer ISO, not
  ``ssh-copy-id``: sshd reads ``administrators_authorized_keys`` for every
  member of Administrators, so a key copied to ``~/.ssh`` would "succeed"
  and still not work;
- its inventory host gets ``ansible_shell_type: powershell``;
- the clone-identity pass, which is Linux-only, is off;
- ``cloudinit:`` is refused: cloud-init does not run on Windows.

An ``os: windows`` VM that boots from an ISO is installed unattended:
boxman renders its answer file (``unattend:``, a Jinja template, its own by
default) and builds the answer ISO itself, with that file, the setup
script and the public keys. The ISO holds the Administrator password in
plain text, so it is written 0600 from a private staging directory, lives
in the cluster workdir beside the VM's disk and never in the box, is an
install-only cdrom (ejected once the install is done, G7), and is removed
then, or with the VM.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from importlib import resources as importlib_resources
from typing import Any
from xml.etree import ElementTree as ET

import jinja2

from boxman.exceptions import ConfigError, ProvisionError
from boxman.providers.libvirt.install_media import INSTALL_ONLY_KEY
from boxman.providers.libvirt.iso_image import build_iso
from boxman.utils.jinja_env import create_jinja_env
from boxman.utils.shell import run

#: the values ``os:`` takes; a VM without one is ``linux``
GUEST_OSES = ('linux', 'windows')

#: the ssh user of an ``os: windows`` VM that names no ``admin_user``
WINDOWS_ADMIN = 'Administrator'

#: the keys an ``unattend:`` block takes
UNATTEND_KEYS = frozenset({'template', 'files', 'image_index', 'vars'})

#: the names boxman writes onto an answer ISO itself, which no file of
#: ``unattend: files:`` may take
ANSWER_FILE = 'autounattend.xml'
AUTHORIZED_KEYS = 'authorized_keys'

#: the volume label of an answer ISO. Windows Setup looks for
#: ``autounattend.xml`` on every removable drive, whatever its label.
ANSWER_ISO_LABEL = 'BOXMAN_ANSWER'

#: the ``name:`` of the cdrom entry boxman adds for a VM's answer ISO
ANSWER_CDROM_NAME = 'unattend'

#: marks that entry as boxman's, so it is added once
ANSWER_CDROM_MARK = '_answer_iso'

#: a NetBIOS computer name: Setup stops at a prompt for anything else
_NETBIOS_NAME = re.compile(r'[A-Za-z0-9-]{1,15}')


def windows_assets() -> str:
    """The directory of boxman's default answer file and setup script."""
    return str(importlib_resources.files('boxman').joinpath('assets',
                                                            'windows'))


def default_answer_template() -> str:
    return os.path.join(windows_assets(), 'autounattend.xml.j2')


def default_answer_files() -> list[str]:
    return [os.path.join(windows_assets(), 'boxman-setup.ps1')]


def answer_iso_path(workdir: str, full_vm_name: str) -> str:
    """Where a VM's answer ISO lives: beside its boot disk, in the cluster
    workdir -- never under the box, which may be a git checkout.

    Not named ``seed*``: CDROMManager takes those for cloud-init seeds and
    leaves them out of every comparison.
    """
    return os.path.join(os.path.abspath(os.path.expanduser(workdir)),
                        f"{full_vm_name}-unattend.iso")


def _git_work_tree(path: str) -> str | None:
    """The git work tree *path* is in, or None."""
    path = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(path, '.git')):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def password_problem(password: str, users: tuple[str, ...]) -> str | None:
    """
    What makes *password* fail Windows' default password policy, or None.

    That policy is enforced at OOBE, where an unattended install meets a
    password it refuses with a prompt nobody answers: the guest waits there,
    and ``boxman up`` waits for an address it never gets. So the rule is
    checked before anything is built: eight characters or more, three of
    upper case, lower case, digit and symbol, and none of the account names.
    The password itself is never part of the answer.
    """
    classes = sum(bool(re.search(pattern, password)) for pattern in
                  (r'[A-Z]', r'[a-z]', r'[0-9]', r'[^A-Za-z0-9]'))
    if len(password) < 8 or classes < 3:
        return ("is shorter than 8 characters or has fewer than 3 of upper "
                "case, lower case, digits and symbols, which Windows refuses")
    for user in users:
        if len(user) > 2 and user.lower() in password.lower():
            return (f"contains the account name '{user}', which Windows "
                    f"refuses")
    return None


def render_answer_file(template_path: str, context: dict[str, Any]) -> str:
    """
    Render an answer-file template, autoescaped for XML.

    The environment is conf.yml's -- ``env()``, ``env_required()``,
    ``env_is_set()`` -- with every value escaped, since the file is XML and
    a password may hold ``&`` or ``<``, and an undefined name is an error,
    not an empty string. The result is checked for well-formedness: Setup
    rejects a malformed answer file only at boot, minutes in. No error
    carries the rendered text, which holds the password.

    Raises:
        ConfigError: the template does not render, or not into XML.
    """
    jinja_env = create_jinja_env(os.path.dirname(template_path))
    jinja_env.autoescape = True
    jinja_env.undefined = jinja2.StrictUndefined
    try:
        text = jinja_env.get_template(
            os.path.basename(template_path)).render(**context)
    except ConfigError:
        raise
    except jinja2.TemplateError as exc:
        raise ConfigError(
            f"the answer file template {template_path} did not render: "
            f"{exc}") from exc
    try:
        # the user's own template, rendered here: expat fetches no external
        # entity, and an entity bomb would only hang this run
        ET.fromstring(text.encode('utf-8'))
    except ET.ParseError as exc:
        raise ConfigError(
            f"the answer file rendered from {template_path} is not "
            f"well-formed XML ({exc}); Windows Setup would refuse it only "
            f"at boot") from exc
    return text


def write_answer_iso(iso_path: str, generated: dict[str, str],
                     files: list[str]) -> str:
    """
    Build the answer ISO at *iso_path*, readable by its owner only.

    Everything is staged in a fresh 0700 directory beside *iso_path*,
    written 0600, and the image is moved into place once complete, so no
    one else can read the rendered files or the image at any point, and
    the guest never sees a half-written one. The staging directory is
    removed whatever happens. (The cloud-init seed has no such policy yet,
    #197.)

    0600 is enough for qemu: libvirt's DAC driver gives a domain's disks,
    read-only ones included, to the qemu user when it starts the domain
    (``dynamic_ownership``, on by default), and does not give a read-only
    one back. A host with ``dynamic_ownership = 0`` cannot start the VM.

    The ISO tools run here, as the user, not through virsh: ISO boot is
    local-runtime only, and an image built through sudo would be root's.

    Args:
        generated: name -> text of the files boxman writes (the answer
            file, the public keys).
        files: paths copied onto the ISO as they are, under their basename.

    Returns:
        The tool that built it.

    Raises:
        ProvisionError: no ISO tool could build it.
    """
    workdir = os.path.dirname(iso_path)
    os.makedirs(workdir, exist_ok=True)
    stage = tempfile.mkdtemp(prefix='.unattend-', dir=workdir)
    try:
        staged = []
        for name, text in generated.items():
            path = os.path.join(stage, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fobj:
                fobj.write(text)
            staged.append(path)
        for source in files:
            path = os.path.join(stage, os.path.basename(source))
            shutil.copyfile(source, path)
            os.chmod(path, 0o600)
            staged.append(path)
        image = os.path.join(stage, 'answer.iso')
        tool = build_iso(run, image, ANSWER_ISO_LABEL, staged)
        if tool is None:
            raise ProvisionError(
                f"could not build the answer ISO {iso_path}: install "
                f"genisoimage, mkisofs or xorrisofs")
        os.chmod(image, 0o600)
        os.replace(image, iso_path)
        return tool
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class GuestOSMixin:

    @staticmethod
    def guest_os(vm_info: dict[str, Any] | None) -> str:
        """The VM's ``os:``, ``linux`` when it has none."""
        return (vm_info or {}).get('os') or 'linux'

    @classmethod
    def is_windows(cls, vm_info: dict[str, Any] | None) -> bool:
        return cls.guest_os(vm_info) == 'windows'

    @classmethod
    def ssh_user(cls, cluster: dict[str, Any], vm_info: dict[str, Any],
                 default: str) -> str:
        """
        The user boxman's ssh config and connection info name for a VM.

        A Linux VM's is the cluster's ``admin_user``, or *default* as each
        caller always had it. An ``os: windows`` VM's is its own
        ``admin_user``, the cluster's, or ``Administrator``: the account
        the answer file sets the password of, and the one sshd lets in with
        ``administrators_authorized_keys``.
        """
        if cls.is_windows(vm_info):
            return (vm_info.get('admin_user') or cluster.get('admin_user')
                    or WINDOWS_ADMIN)
        return cluster.get('admin_user', default)

    @classmethod
    def unattend_of(cls, vm_info: dict[str, Any]) -> Any:
        """
        The ``unattend:`` block of a VM that is installed unattended, or
        None for one that is not.

        That is an ``os: windows`` VM booting from an ISO, unless it says
        ``unattend: false`` -- an install by hand, over VNC. Omitted, or
        ``true``, the block is ``{}``: boxman's own answer file and setup
        script. A malformed block is returned as it is, for
        :meth:`validate_guest_os_config` to refuse.
        """
        if not cls.is_windows(vm_info):
            return None
        boot_order = vm_info.get('boot_order') or ['hd']
        if boot_order[0] != 'cdrom':
            return None
        block = vm_info.get('unattend')
        if block is False:
            return None
        if block is None or block is True:
            return {}
        return block

    def _conf_dir(self) -> str:
        """Where a relative ``unattend:`` path is read from: the directory
        of conf.yml."""
        if getattr(self, 'config_path', None):
            return os.path.dirname(os.path.abspath(self.config_path))
        return os.environ.get('BOXMAN_CONF_DIR') or os.getcwd()

    def _unattend_path(self, path: str) -> str:
        return os.path.normpath(os.path.join(
            self._conf_dir(), os.path.expanduser(str(path))))

    def add_answer_cdroms(self) -> None:
        """
        Give every VM installed unattended the cdrom entry of its answer ISO.

        Install-only, with an explicit ``source:`` in the cluster workdir,
        and added to the config itself rather than by each reader, so every
        verb sees one list: the create path hands it to virt-install, the
        eject finds the drive by it, and ``update`` leaves it out of its
        comparison. The file itself is built only when the VM is created
        (:meth:`build_answer_media`). Runs once the workdirs are resolved;
        a second run adds nothing.
        """
        project = (self.config or {}).get('project')
        for cluster_name, cluster in ((self.config or {}).get('clusters')
                                      or {}).items():
            workdir = (cluster or {}).get('workdir')
            if not workdir:
                continue
            for vm_name, vm_info in ((cluster or {}).get('vms') or {}).items():
                if (not isinstance(vm_info, dict)
                        or self.unattend_of(vm_info) is None):
                    continue
                cdroms = vm_info.setdefault('cdroms', [])
                if not isinstance(cdroms, list) or any(
                        isinstance(c, dict) and c.get(ANSWER_CDROM_MARK)
                        for c in cdroms):
                    continue
                full = f"bprj__{project}__bprj_{cluster_name}_{vm_name}"
                cdroms.append({
                    'name': ANSWER_CDROM_NAME,
                    'source': answer_iso_path(workdir, full),
                    INSTALL_ONLY_KEY: True,
                    ANSWER_CDROM_MARK: True,
                })

    def validate_guest_os_config(self) -> None:
        """
        Refuse what an ``os:`` value, or an ``os: windows`` VM, cannot do,
        before the command does anything. Configuration only: the password
        is checked where a VM is about to be created
        (:meth:`validate_answer_media`), since a VM that exists no longer
        needs one.

        Raises:
            ConfigError: naming every offending VM at once.
        """
        bad: list[str] = []
        templates = (self.config.get('templates') or {})
        template_names = set(templates) | {
            tpl.get('name') for tpl in templates.values()
            if isinstance(tpl, dict) and tpl.get('name')}
        runtime = getattr(self, '_runtime_name', 'local') or 'local'
        for cluster_name, cluster in (self.config.get('clusters')
                                      or {}).items():
            for vm_name, vm_info in ((cluster or {}).get('vms')
                                     or {}).items():
                if not isinstance(vm_info, dict):
                    continue
                loc = f"{cluster_name}.vms.{vm_name}"
                value = vm_info.get('os', 'linux')
                if value not in GUEST_OSES:
                    bad.append(f"{loc}.os must be one of "
                               f"{', '.join(GUEST_OSES)}, got {value!r}")
                    continue
                if value != 'windows':
                    if 'unattend' in vm_info:
                        bad.append(f"{loc}.unattend is for an os: windows "
                                   f"VM")
                    continue
                bad.extend(self._windows_vm_problems(
                    loc, cluster or {}, vm_info, template_names, runtime))
        if bad:
            raise ConfigError(
                "invalid guest os configuration: " + "; ".join(bad))

    def _windows_vm_problems(self, loc: str, cluster: dict[str, Any],
                             vm_info: dict[str, Any], template_names: set,
                             runtime: str) -> list[str]:
        """The reasons one ``os: windows`` VM cannot be provisioned."""
        bad = []
        for key in sorted(vm_info):
            if key.startswith('cloudinit'):
                bad.append(
                    f"{loc}.{key}: cloud-init does not run on Windows; an "
                    f"os: windows VM is set up by its answer file "
                    f"(unattend:)")
        boot_order = vm_info.get('boot_order') or ['hd']
        cloned = boot_order[0] not in ('cdrom', 'network')
        base = vm_info.get('base_image') or cluster.get('base_image')
        if cloned and base in template_names:
            bad.append(
                f"{loc}: base_image '{base}' is a templates: entry, which "
                f"boxman builds with cloud-init, and cloud-init does not run "
                f"on Windows")
        for key in ('clone_machine_id', 'clone_ssh_host_keys',
                    'clone_hostname'):
            if vm_info.get(key) == 'required':
                bad.append(
                    f"{loc}.{key} is required, but the clone identity pass is "
                    f"Linux-only and is off for an os: windows VM")

        if 'unattend' in vm_info and vm_info['unattend'] is not None and \
                not isinstance(vm_info['unattend'], bool | dict):
            bad.append(f"{loc}.unattend must be a mapping, true or false")
            return bad
        if (isinstance(vm_info.get('unattend'), dict)
                or vm_info.get('unattend') is True) and boot_order[0] != 'cdrom':
            bad.append(f"{loc}.unattend needs a VM that boots from an ISO "
                       f"(boot_order starting with cdrom)")
            return bad
        block = self.unattend_of(vm_info)
        if block is None:
            return bad
        if runtime != 'local':
            bad.append(f"{loc}: an unattended install needs the local "
                       f"runtime (the answer ISO is built on this host)")
        unknown = sorted(set(block) - UNATTEND_KEYS)
        if unknown:
            bad.append(f"{loc}.unattend has unknown key(s) "
                       f"{', '.join(unknown)} (it takes "
                       f"{', '.join(sorted(UNATTEND_KEYS))})")
        template = block.get('template')
        if template is not None:
            if not isinstance(template, str) or not template:
                bad.append(f"{loc}.unattend.template must be a path")
            elif not os.path.isfile(self._unattend_path(template)):
                bad.append(f"{loc}.unattend.template "
                           f"{self._unattend_path(template)} does not exist")
        files = block.get('files')
        if files is not None:
            if not isinstance(files, list) or not all(
                    isinstance(f, str) and f for f in files):
                bad.append(f"{loc}.unattend.files must be a list of paths")
            else:
                seen: set = set()
                for path in files:
                    full = self._unattend_path(path)
                    name = os.path.basename(full)
                    if not os.path.isfile(full):
                        bad.append(f"{loc}.unattend.files: {full} does not "
                                   f"exist")
                    elif name in (ANSWER_FILE, AUTHORIZED_KEYS):
                        bad.append(f"{loc}.unattend.files: {name} is the "
                                   f"name of a file boxman writes itself")
                    elif name in seen:
                        bad.append(f"{loc}.unattend.files: two files are "
                                   f"named {name}")
                    seen.add(name)
        index = block.get('image_index', 1)
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            bad.append(f"{loc}.unattend.image_index must be a positive "
                       f"integer, got {index!r}")
        if not isinstance(block.get('vars', {}), dict):
            bad.append(f"{loc}.unattend.vars must be a mapping")
        return bad

    def _answer_targets(self, full_vm_names=None):
        """``(cluster name, cluster, vm key, vm info, full name)`` of each
        VM installed unattended, among *full_vm_names* (None: all)."""
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self._vm_clusters.items():
            for vm_name, vm_info in (cluster.get('vms') or {}).items():
                full = f"{prj_name}_{cluster_name}_{vm_name}"
                if full_vm_names is not None and full not in full_vm_names:
                    continue
                if isinstance(vm_info, dict) and \
                        self.unattend_of(vm_info) is not None:
                    yield cluster_name, cluster, vm_name, vm_info, full

    def _answer_context(self, cluster_name: str, cluster: dict[str, Any],
                        vm_name: str, vm_info: dict[str, Any]
                        ) -> dict[str, Any]:
        """
        What the answer file of one VM is rendered with.

        Raises:
            ConfigError: no usable ``admin_pass``, or no usable computer
                name. Neither message holds the password.
        """
        loc = f"{cluster_name}.vms.{vm_name}"
        raw = vm_info.get('admin_pass', cluster.get('admin_pass'))
        if raw is None or raw is False or raw == '':
            raise ConfigError(
                f"{loc} is installed unattended, so it needs admin_pass, the "
                f"Administrator password, on the VM or its cluster; it is "
                f"not set or empty. Keep it out of the box: admin_pass: "
                f"'{{{{ env(\"BOXMAN_WIN_ADMIN_PASS\") }}}}'")
        try:
            password = str(self.fetch_value(raw))
        except (ValueError, OSError) as exc:
            raise ConfigError(
                f"the admin_pass of {loc} cannot be resolved ({exc})") from exc
        admin_user = self.ssh_user(cluster, vm_info, WINDOWS_ADMIN)
        problem = password_problem(
            password, tuple(dict.fromkeys((WINDOWS_ADMIN, admin_user))))
        if problem:
            raise ConfigError(f"the admin_pass of {loc} {problem}")
        computer_name = str(vm_info.get('hostname') or vm_name)
        if not _NETBIOS_NAME.fullmatch(computer_name) or computer_name.isdigit():
            raise ConfigError(
                f"{loc}: '{computer_name}' is not a Windows computer name (1 "
                f"to 15 letters, digits and hyphens, not all digits); set "
                f"hostname:")
        block = self.unattend_of(vm_info) or {}
        return {
            'admin_user': admin_user,
            'admin_pass': password,
            'computer_name': computer_name,
            'image_index': block.get('image_index', 1),
            'vm_name': vm_name,
            **(block.get('vars') or {}),
        }

    def validate_answer_media(self, full_vm_names=None) -> None:
        """
        Check what the answer ISOs of the VMs about to be created need,
        before anything is built: a password Windows accepts, a computer
        name, and an answer file that renders into XML.

        Raises:
            ConfigError: for the first VM that fails.
        """
        for cluster_name, cluster, vm_name, vm_info, _full in \
                self._answer_targets(full_vm_names):
            context = self._answer_context(cluster_name, cluster, vm_name,
                                           vm_info)
            render_answer_file(self._answer_template(vm_info), context)

    def _answer_template(self, vm_info: dict[str, Any]) -> str:
        template = (self.unattend_of(vm_info) or {}).get('template')
        return (self._unattend_path(template) if template
                else default_answer_template())

    def _answer_files(self, vm_info: dict[str, Any]) -> list[str]:
        files = (self.unattend_of(vm_info) or {}).get('files')
        if files is None:
            return default_answer_files()
        return [self._unattend_path(path) for path in files]

    def build_answer_media(self, full_vm_names=None) -> None:
        """
        Build the answer ISO of every VM about to be created that is
        installed unattended.

        It holds the rendered answer file, the ``unattend: files:`` (boxman's
        ``boxman-setup.ps1`` by default) and ``authorized_keys``: the
        cluster's public key, generated here if it does not exist yet, and
        boxman.yml's ``ssh.authorized_keys``. The setup script writes them
        to ``administrators_authorized_keys``.

        Raises:
            ConfigError: see :meth:`_answer_context` and
                :func:`render_answer_file`.
            ProvisionError: the key pair or the ISO could not be made.
        """
        targets = list(self._answer_targets(full_vm_names))
        if not targets:
            return
        # The key goes into the guest with the answer ISO, which is built
        # before the VM exists -- for a Linux VM the pair is only needed
        # once it runs, and was generated then.
        if not self.generate_ssh_keys():
            raise ProvisionError(
                "the cluster ssh key pair could not be generated, and the "
                "answer ISO has to carry its public key")
        global_keys = self.get_global_authorized_keys()
        ws_path = self.config.get('workspace', {}).get('path', '')
        for cluster_name, cluster, vm_name, vm_info, full in targets:
            label = f"{cluster_name}/{vm_name}"
            context = self._answer_context(cluster_name, cluster, vm_name,
                                           vm_info)
            answer = render_answer_file(self._answer_template(vm_info),
                                        context)
            pub_key = os.path.join(
                os.path.expanduser(ws_path or cluster['workdir']),
                f"{cluster.get('admin_key_name', 'id_ed25519_boxman')}.pub")
            try:
                with open(pub_key, encoding='utf-8') as fobj:
                    keys = [fobj.read().strip()]
            except OSError as exc:
                raise ProvisionError(
                    f"could not read the public key {pub_key} for the answer "
                    f"ISO of vm {label}: {exc.strerror or exc}") from exc
            keys += [key.strip() for key in global_keys if key.strip()]
            iso = answer_iso_path(cluster['workdir'], full)
            repo = _git_work_tree(os.path.dirname(iso))
            if repo:
                self.logger.warning(
                    f"vm {label}: its answer ISO {iso}, which holds the "
                    f"Administrator password, is inside the git work tree "
                    f"{repo}; boxman's .gitignore covers *-unattend.iso, a "
                    f"repository of your own may not. A workdir outside it "
                    f"keeps the password out of every commit.")
            tool = write_answer_iso(
                iso,
                {ANSWER_FILE: answer, AUTHORIZED_KEYS: '\n'.join(keys) + '\n'},
                self._answer_files(vm_info))
            self.logger.info(
                f"vm {label}: wrote its answer ISO {iso} with {tool}, readable "
                f"by its owner only: it holds the Administrator password")

    def remove_answer_media(self, full_vm_name: str, workdirs,
                            strict: bool = True) -> list[str]:
        """
        Remove a VM's answer ISO from whichever of *workdirs* holds one.

        Called once its install-only media are ejected, and with the VM:
        nothing reads the ISO after the install, and it holds the
        Administrator password. libvirt gives a read-only image to qemu's
        user and never gives it back, so the file may no longer be the
        user's -- removing it needs only the directory.

        Args:
            strict: a path that cannot even be looked up fails too. A VM
                removed from the config passes False: every workdir of the
                project is looked in for it, its own unknown, and one that
                cannot be searched is another VM's business (the teardown
                keeps what is under such a directory anyway).

        Returns:
            The paths removed.

        Raises:
            ProvisionError: one is there and cannot be removed, or, strict,
                cannot be looked up.
        """
        removed = []
        for workdir in workdirs:
            path = answer_iso_path(workdir, full_vm_name)
            try:
                os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if strict:
                    raise ProvisionError(
                        f"could not look up the answer ISO {path} of "
                        f"{full_vm_name}, which may hold its Administrator "
                        f"password: {exc.strerror or exc}") from exc
                self.logger.warning(
                    f"could not look up {path} ({exc.strerror or exc}); if "
                    f"it is there, it is the answer ISO of {full_vm_name} and "
                    f"holds its Administrator password: remove it by hand")
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProvisionError(
                    f"could not remove the answer ISO {path}, which holds the "
                    f"Administrator password of {full_vm_name}: "
                    f"{exc.strerror or exc}") from exc
            self.logger.info(f"removed the answer ISO {path}")
            removed.append(path)
        return removed
