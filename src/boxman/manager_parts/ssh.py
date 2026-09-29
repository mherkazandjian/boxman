"""SSH keys, ssh config, and connect-info helpers for BoxmanManager."""





import ipaddress
import os
import re
import shlex
import time
from collections import Counter

from boxman.exceptions import SSHAccessError
from boxman.utils.hostnames import hostname_or_key
from boxman.utils.references import resolve_reference
from boxman.utils.shell import run

#: The directives of the VM blocks :meth:`SSHMixin.write_ssh_config`
#: writes, each once. ``proxyjump`` is written under the docker runtime
#: only; the others always.
_VM_BLOCK_REQUIRED = frozenset({
    'hostname', 'user', 'identityfile', 'stricthostkeychecking',
    'userknownhostsfile',
})

#: The second Host alias write_ssh_config gives a VM: ``node`` and a
#: zero-padded project-wide counter.
_NODE_ALIAS = re.compile(r'node[0-9]+')

#: What no ssh_config spelling of a path carries through OpenSSH as it
#: stands: a double quote ends the quoted argument and a line break the
#: line; ``${`` is expanded as an environment variable, with no escape for
#: it; and a backslash before a backslash or a quote, or at the end, is an
#: escape from OpenSSH 8.7 on but literal before it, so no one spelling
#: suits both. Every other backslash is literal to both.
_UNWRITABLE_IN_PATH = re.compile(r'["\n\r]|\$\{|\\(?=[\\\'"]|$)')


class _UnwritablePathError(ValueError):
    """A path no ssh_config line can hold so that OpenSSH reads it back."""


def _path_argument(path: str) -> str:
    """
    *path* as one ssh_config argument that OpenSSH reads back as *path*.

    In double quotes, so a blank in it -- in the workspace, a workdir, the
    ``admin_key_name`` -- does not split it into arguments, which OpenSSH
    refuses outright ("extra arguments at end of line"); and with each
    ``%`` doubled, since OpenSSH expands %-tokens in IdentityFile and reads
    ``%%`` as a literal ``%`` (#223).

    Raises:
        _UnwritablePathError: *path* holds something no spelling carries (see
            ``_UNWRITABLE_IN_PATH``).
    """
    found = _UNWRITABLE_IN_PATH.search(path)
    if found:
        what = {'"': 'a double quote', '\n': 'a line break',
                '\r': 'a line break', '${': "'${'"}.get(
                    found.group(), 'a backslash before a quote or a '
                                   'backslash, or at its end')
        raise _UnwritablePathError(
            f"the path {path!r} cannot be written into it: OpenSSH does not "
            f"read {what} in a path back as written")
    return '"' + path.replace('%', '%%') + '"'


def _config_words(line: str) -> list[str] | None:
    """
    The words OpenSSH reads from one ssh_config *line*: separated by blanks,
    a double- or single-quoted run part of one word, its quotes dropped. So
    ``IdentityFile "/work space/id"`` is two words. None for a quote left
    open, which OpenSSH refuses. A backslash stands as it is: boxman never
    writes one OpenSSH would read as an escape (see _path_argument).
    """
    words: list[str] = []
    word: list[str] | None = None
    quote = None
    for char in line:
        if quote:
            if char == quote:
                quote = None
            else:
                word.append(char)
        elif char in ' \t':
            if word is not None:
                words.append(''.join(word))
                word = None
        else:
            if word is None:
                word = []
            if char in '"\'':
                quote = char
            else:
                word.append(char)
    if quote:
        return None
    if word is not None:
        words.append(''.join(word))
    return words


def _block_address(body: list[tuple[str, list[str]]]) -> str | None:
    """
    The IPv4 ``Hostname`` of a VM block's *body* -- each line raw and as
    :func:`_config_words` reads it -- if the body is exactly what
    write_ssh_config writes, or None.

    Every directive it writes, once, with the fixed values it writes --
    ``StrictHostKeyChecking no``, ``UserKnownHostsFile /dev/null`` -- and
    ``ProxyJump`` to the docker runtime's jump host at most once; one
    argument each; nothing else. ``IdentityFile`` alone may also take the
    rest of the line: boxman wrote the path unquoted before it quoted it,
    so a file written before the upgrade keeps its blocks, and the rewrite
    quotes them.
    """
    jump = SSHMixin.SSH_JUMP_HOST_ALIAS
    values: dict[str, str] = {}
    for raw, words in body:
        keyword = words[0].lower()
        if len(words) == 2:
            argument = words[1]
        elif (keyword == 'identityfile' and len(words) > 2
              and not {'"', "'"} & set(raw)):
            argument = raw.split(None, 1)[1].strip()
        else:
            return None
        if (keyword not in _VM_BLOCK_REQUIRED | {'proxyjump'}
                or keyword in values):
            return None
        values[keyword] = argument
    if (not _VM_BLOCK_REQUIRED <= values.keys()
            or values['stricthostkeychecking'] != 'no'
            or values['userknownhostsfile'] != '/dev/null'
            or values.get('proxyjump', jump) != jump):
        return None
    try:
        return str(ipaddress.IPv4Address(values['hostname']))
    except ValueError:
        return None


def _normal_address(address: str) -> str:
    """*address* spelled the way earlier_vm_addresses spells one."""
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return address


def earlier_vm_addresses(path: str) -> dict[str, str]:
    """
    The address each VM block of the ssh config at *path* gives, keyed by
    the block's first ``Host`` alias (``<cluster>_<hostname>``; the second,
    ``nodeN``, shifts when VMs are added or removed).

    Only what :meth:`SSHMixin.write_ssh_config` writes is read: a
    ``Host <alias> node<digits>`` line, then exactly the directives it
    writes (see :func:`_block_address`), the ``Hostname`` an IPv4 address;
    comment lines are skipped. Arguments are read as OpenSSH reads them, a
    quoted one being one word. A block that deviates is left out, and so
    is one whose aliases another ``Host`` line names too -- whatever else
    that line holds -- since which of them described the VM cannot be
    told. A file that cannot be read or decoded, or that leaves a quote
    open, yields nothing. An earlier block that cannot be understood is no
    earlier block (#223).
    """
    try:
        with open(path, encoding='utf-8') as fobj:
            text = fobj.read()
    except (OSError, UnicodeError):
        return {}

    blocks: list[tuple[list[str], list[tuple[str, list[str]]]]] = []
    for raw in text.splitlines():
        # a comment line is skipped before its words are read, as OpenSSH
        # skips it: an apostrophe in one opens no quote
        if not raw.strip() or raw.strip().startswith('#'):
            continue
        words = _config_words(raw)
        if words is None:
            return {}
        if words[0].lower() in ('host', 'match'):
            blocks.append((words, []))
        elif blocks:
            blocks[-1][1].append((raw, words))

    # every alias of every Host line, counted before any block is judged:
    # a competing definition makes an alias ambiguous whatever its shape
    named = Counter(alias for head, _ in blocks
                    if head[0].lower() == 'host' for alias in head[1:])
    found: dict[str, str] = {}
    for head, body in blocks:
        if (head[0].lower() != 'host' or len(head) != 3
                or not _NODE_ALIAS.fullmatch(head[2])
                or named[head[1]] != 1 or named[head[2]] != 1):
            continue
        address = _block_address(body)
        if address:
            found[head[1]] = address
    return found


def _no_password(value) -> bool:
    """Whether an ``admin_pass`` value means "no password".

    Absent, empty, or YAML's ``no``. Not ``0``: an all-digit password comes
    back from yaml.safe_load as an int, and 0 is falsy.
    """
    return value is None or value is False or value == ''


class SSHMixin:

    def get_connect_info(self) -> bool:
        """
        Gather connection information for all VMs in all clusters.

        Queries all VMs in parallel and returns True only if every VM has at
        least one IP address.

        Returns:
            True if all VMs have at least one IP address, False otherwise
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        vm_names = [
            f"{prj_name}_{cluster_name}_{vm_name}"
            for cluster_name, cluster in self._vm_clusters.items()
            for vm_name in cluster['vms']
        ]

        def _check(full_vm_name):
            return self.session_for_vm(full_vm_name).get_vm_ip_addresses(full_vm_name)

        # Routed through _run_parallel: a crashed child can no longer
        # deadlock the parent on a blocking result_queue.get().
        results, _failures = self._run_parallel(
            [(n, _check, (n,)) for n in vm_names],
            op_label='ip address check')

        all_vms_have_ip = True
        for full_vm_name in vm_names:
            ips = results.get(full_vm_name)
            if not ips:
                all_vms_have_ip = False
                self.logger.warning(f"vm {full_vm_name} does not have an ip address yet")

        return all_vms_have_ip

    def connect_info(self) -> None:
        """
        Display connection information for all VMs in all clusters.

        This method displays the VM names, hostnames, IP addresses, and
        other connection details for all configured VMs.
        """
        self.logger.status("=== vm connection information ===")
        ws_path = self.config.get('workspace', {}).get('path', '')

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self._vm_clusters.items():
            self.logger.status(f"cluster: {cluster_name}")

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                hostname = hostname_or_key(vm_name, vm_info)

                self.logger.status(f"vm: {vm_name} (hostname: {hostname})")

                # get the ip addresses for all interfaces
                ip_addresses = self.session_for_cluster(cluster_name).get_vm_ip_addresses(full_vm_name)

                if ip_addresses:
                    self.logger.status("  ip addresses:")
                    for iface, ip in ip_addresses.items():
                        self.logger.status(f"    {iface}: {ip}")
                else:
                    self.logger.status("  ip addresses: not available")

                # get ssh connection information
                admin_user = cluster.get('admin_user', '<placeholder>')
                base_path = ws_path or cluster.get('workdir', '~')
                admin_key = os.path.expanduser(os.path.join(
                    base_path,
                    cluster.get('admin_key_name', 'id_ed25519_boxman')
                ))

                self.logger.status("  connect via ssh:")
                # show direct connection if ip is available; the paths are
                # quoted so the command can be pasted when they hold a blank
                if ip_addresses:
                    first_ip = next(iter(ip_addresses.values()))
                    self.logger.status(
                        f"    direct: ssh -i {shlex.quote(admin_key)} "
                        f"{admin_user}@{first_ip}")

                # show connection using ssh_config if available
                if 'ssh_config' in cluster:
                    ssh_config = os.path.expanduser(os.path.join(
                        base_path,
                        cluster.get('ssh_config', 'ssh_config')
                    ))
                    self.logger.status(
                        f"    via config: ssh -F {shlex.quote(ssh_config)} "
                        f"{cluster_name}_{hostname}")

                self.logger.status("")

            self.logger.status("")

        # docker-compose clusters: container status + published ports + the
        # exec entry point (containers are reached with `boxman exec`, not ssh).
        for cluster_name, cluster in self._compose_clusters.items():
            self.logger.info(f"cluster: {cluster_name} (docker-compose)")
            self.logger.info("-" * 60)
            try:
                status = {
                    r["service"]: r for r in
                    self._dc_session(cluster_name).container_status(
                        cluster_name, cluster)
                }
            except Exception as exc:
                self.logger.warning(f"  could not query containers: {exc}")
                status = {}
            for box_name in (cluster.get("boxes") or {}):
                row = status.get(box_name, {})
                state = row.get("state", "not created")
                health = f" ({row['health']})" if row.get("health") else ""
                self.logger.info(f"container: {box_name}  [{state}{health}]")
                if row.get("ports"):
                    self.logger.info(f"  published ports: {row['ports']}")
                self.logger.info(f"  connect: boxman exec {cluster_name}.{box_name}")
                self.logger.info("")
            self.logger.info("")

    #: Alias of the ProxyJump stanza written into ``ssh_config`` when the
    #: docker runtime is active. Kept as a class constant so tests and
    #: downstream tooling can grep for it reliably.
    SSH_JUMP_HOST_ALIAS = "boxman-libvirt-jump"

    def _docker_ssh_jump_stanza(self) -> str | None:
        """
        Return the ``Host boxman-libvirt-jump`` block to prepend to
        ``ssh_config`` when the docker runtime is active, or ``None``
        when the runtime is ``local`` (VMs are directly reachable from
        the host in that case).
        """
        from boxman.runtime.docker_compose import DockerComposeRuntime

        if not isinstance(self.runtime_instance, DockerComposeRuntime):
            return None

        rt = self.runtime_instance
        # quoted like every path boxman writes here: the project directory
        # this lives under may have a blank in it (#223)
        return (
            f"Host {self.SSH_JUMP_HOST_ALIAS}\n"
            f"    HostName     127.0.0.1\n"
            f"    Port         {rt.ssh_port}\n"
            f"    User         qemu_user\n"
            f"    IdentityFile {_path_argument(rt.ssh_identity_path)}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"\n\n"
        )

    def write_ssh_config(self,
                         vm_states: dict[str, str] | None = None,
                         fresh=(),
                         adding_keys: bool = False) -> None:
        """
        Generate SSH configuration file for easy access to VMs.

        Creates an SSH config file in the workspace directory that allows
        simplified access to VMs without typing full connection details.

        Under the docker runtime, a ``Host boxman-libvirt-jump`` stanza
        is prepended and each VM block gets a ``ProxyJump`` directive so
        that host-side ``ssh``, ``scp`` and ansible transparently hop
        through the libvirt container to reach VMs on libvirt's internal
        NAT network.

        A VM that is not running keeps the block it had in the file being
        rewritten, address included, marked as coming from an earlier run;
        one WARNING per such VM says so, or says why it has no block (#223).

        Args:
            vm_states: full VM name -> state, as
                :meth:`_vm_states_if_known` gives it. A VM known not to be
                running is not asked for its address: it has none, and
                asking makes the session warn. None -- the states are
                unknown -- asks every VM, and one without an address gets
                no block, as before.
            fresh: full names of the VMs created in this run. An earlier
                block under their alias describes a domain that is gone,
                so it is never reused.
            adding_keys: this run also adds the admin key to the VMs (the
                ssh setup of ``update`` and ``provision``), so the warning
                for a VM that is not running, in a cluster with an
                ``admin_pass``, says its key was not added.

        Raises:
            SSHAccessError: a file could not be written; the others are.
        """
        ws_path = self.config.get('workspace', {}).get('path', '')
        prj_name = f'bprj__{self.config["project"]}__bprj'
        fresh = set(fresh)

        # count total VMs across all clusters for zero-padded alias numbering
        total_vms = sum(
            len(cluster.get('vms', {}))
            for cluster in self.config['clusters'].values()
        )
        pad_width = len(str(total_vms - 1)) if total_vms > 1 else 1
        vm_counter = 0

        # the docker runtime's jump stanza goes into every file, so a path
        # of its that no file can hold stops every one of them (below)
        jump_problem: _UnwritablePathError | None = None
        try:
            jump_stanza = self._docker_ssh_jump_stanza()
        except _UnwritablePathError as exc:
            jump_stanza, jump_problem = None, exc

        # Group clusters by their resolved ssh_config path. When
        # workspace.path is set every cluster resolves to the SAME path,
        # so we must write each unique path once with all relevant VM
        # blocks — opening 'w' once per cluster would have the later
        # iteration truncate the earlier one's entries.
        groups: dict[str, list[tuple[str, dict, str]]] = {}
        for cluster_name, cluster in self._vm_clusters.items():
            base_path = ws_path or cluster.get('workdir', '~')
            ssh_config = os.path.expanduser(os.path.join(
                base_path,
                cluster.get('ssh_config', 'ssh_config')
            ))
            groups.setdefault(ssh_config, []).append(
                (cluster_name, cluster, base_path)
            )

        # Every VM's entry is settled before any file is written: whether a
        # VM that is not running may keep its earlier address depends on the
        # addresses the running VMs report, in every file.
        plans: list[tuple[str, list[dict]]] = []
        live: dict[str, str] = {}   # address -> the running VM reporting it
        for ssh_config, clusters_in_group in groups.items():
            earlier = ({} if vm_states is None
                       else earlier_vm_addresses(ssh_config))
            entries: list[dict] = []

            for cluster_name, cluster, base_path in clusters_in_group:
                admin_priv_key = os.path.expanduser(os.path.join(
                    base_path,
                    cluster.get('admin_key_name', 'id_ed25519_boxman')
                ))
                # without an admin_pass no verb adds a key to these VMs, so
                # their warning must not promise one
                keys = adding_keys and not _no_password(cluster.get('admin_pass'))

                for vm_name, vm_info in cluster['vms'].items():
                    full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    hostname = hostname_or_key(vm_name, vm_info)
                    prefixed_host = f"{cluster_name}_{hostname}"
                    padded_alias = f"node{str(vm_counter).zfill(pad_width)}"
                    vm_counter += 1
                    entry = {
                        'vm_name': vm_name,
                        'label': f"{cluster_name}/{vm_name}",
                        'host': f"{prefixed_host} {padded_alias}",
                        'user': cluster.get("admin_user", "admin"),
                        'identity_file': admin_priv_key,
                        'keys': keys,
                        'address': None,
                        # set only for a VM known not to be running
                        'state': None,
                        'earlier': None,
                        'why_none': None,
                    }
                    entries.append(entry)

                    state = (None if vm_states is None
                             else vm_states.get(full_vm_name, 'not defined'))
                    if state not in self._NOT_RUNNING_STATES:
                        # get the first ip address if available
                        ip_addresses = self.session_for_cluster(
                            cluster_name).get_vm_ip_addresses(full_vm_name)
                        if ip_addresses:
                            entry['address'] = next(iter(ip_addresses.values()))
                            # every address it reports, not only the one its
                            # block uses: a kept block pointing at any of them
                            # reaches this VM
                            for address in ip_addresses.values():
                                live.setdefault(_normal_address(address),
                                                entry['label'])
                        else:
                            self.logger.warning(
                                f"no ip address available for the vm {vm_name}, "
                                "skipping SSH config entry")
                        continue

                    entry['state'] = state
                    if state == 'not defined':
                        entry['why_none'] = "libvirt has no such domain"
                    elif full_vm_name in fresh:
                        entry['why_none'] = "it was created in this run"
                    elif prefixed_host not in earlier:
                        entry['why_none'] = "there is no earlier entry to keep"
                    else:
                        entry['earlier'] = earlier[prefixed_host]

            plans.append((ssh_config, entries))

        # A VM that is not running keeps the address of its earlier block
        # rather than losing the block (#223). Dropping it meant that once
        # the VM was started outside boxman -- `virsh start`, `boxman control
        # start` -- neither `ssh -F` nor `boxman ssh` could name it until a
        # later up or update saw it running. The kept address is usually
        # still right: a domain keeps its MAC, and libvirt's dnsmasq hands a
        # returning MAC its old lease, or, the lease gone, picks a free
        # address by hashing the MAC; a dhcp.hosts reservation makes it
        # certain. It can be stale all the same, and the block says
        # `StrictHostKeyChecking no`, so ssh would not notice another host
        # answering there. The host that matters is another VM of this
        # project -- same admin user, same key -- where the login would
        # succeed, silently, on the wrong guest. That is what is checked
        # here: an address a running VM reports in this run, or that two
        # kept blocks claim, is not kept. A VM created in this run never
        # inherits a block (`fresh`): what the file says under its alias
        # belongs to a domain that is gone. What is left -- another VM
        # taking the address after this file is written -- is corrected by
        # the next up or update, and is the lesser evil next to a config
        # that cannot reach the VM at all.
        claims = Counter(entry['earlier'] for _, entries in plans
                         for entry in entries if entry['earlier'])
        for _, entries in plans:
            for entry in entries:
                address = entry['earlier']
                if address is None:
                    continue
                if address in live:
                    entry['why_none'] = (
                        f"its earlier address {address} is now "
                        f"{live[address]}'s")
                elif claims[address] > 1:
                    entry['why_none'] = (
                        f"another VM's earlier entry claims its earlier "
                        f"address {address} too")
                else:
                    entry['address'] = address

        unwritten: list[tuple[str, str, Exception]] = []
        for ssh_config, entries in plans:
            self.logger.info(f"writing ssh config to {ssh_config}")
            # The whole text is made before the file is opened: a path no
            # line can hold then leaves the file as it was, rather than
            # truncating it or writing one that OpenSSH refuses (#223).
            cause: Exception | None = jump_problem
            if cause is None:
                try:
                    # docker runtime: jump host stanza
                    text = (jump_stanza or '') + ''.join(
                        self._ssh_config_vm_block(
                            entry, proxy_jump=bool(jump_stanza))
                        for entry in entries if entry['address'] is not None)
                    with open(ssh_config, 'w') as fobj:
                        fobj.write(text)
                except (_UnwritablePathError, OSError) as exc:
                    cause = exc
            if cause is not None:
                reason = (cause.strerror or str(cause)
                          if isinstance(cause, OSError) else str(cause))
                self.logger.error(
                    f"could not write the ssh config {ssh_config}: {reason}")
                unwritten.append((ssh_config, reason, cause))
                continue

            for entry in entries:
                if entry['state'] is not None:
                    self.logger.warning(self._not_running_notice(entry))
            self.logger.info(f"ssh config file written to {ssh_config}")
            self.logger.info(
                f"to connect: ssh -F {shlex.quote(ssh_config)} <hostname>")

        if unwritten:
            raise SSHAccessError([
                f"could not write the ssh config {path}: {reason}"
                for path, reason, _ in unwritten]) from unwritten[0][2]

    def _ssh_config_vm_block(self, entry: dict, proxy_jump: bool) -> str:
        """One VM's block of the ssh config, as write_ssh_config writes it."""
        lines = [f"Host {entry['host']}"]
        if entry['state'] is not None:
            # the VM is not running and keeps its earlier block's address;
            # earlier_vm_addresses skips these lines when it reads it back
            lines += [
                f"    # boxman: {entry['vm_name']} was not running "
                f"({entry['state']}) when this file was written;",
                "    # the address below is from an earlier run and may be "
                "out of date.",
            ]
        lines += [
            f"    Hostname {entry['address']}",
            f"    User {entry['user']}",
            # quoted: the workspace, a workdir or admin_key_name may hold a
            # blank; raises for a path no ssh_config can spell
            f"    IdentityFile {_path_argument(entry['identity_file'])}",
            # per host, never under `Host *`: a boxman VM is recreated often
            # enough that its host key changes under a reused IP, so checking
            # it is noise here. In a `Host *` stanza the same two lines
            # disable checking for every host the reader ever connects to,
            # because OpenSSH takes the first value it sees for a keyword --
            # and this file is meant to be Include-d (#164 CL-S1).
            "    StrictHostKeyChecking no",
            "    UserKnownHostsFile /dev/null",
        ]
        if proxy_jump:
            lines.append(f"    ProxyJump {self.SSH_JUMP_HOST_ALIAS}")
        return "\n".join(lines) + "\n\n\n"

    @staticmethod
    def _not_running_notice(entry: dict) -> str:
        """The one warning for a VM that write_ssh_config found not running.

        It names the verbs that catch up with the VM: ``update`` adds the
        key and rewrites the entry of every VM that is running, ``up`` brings
        a VM up and rewrites the entries but adds no key, and nothing else
        (``control start``, ``virsh start``) touches either.
        """
        if entry['address'] is not None:
            what = (f"its ssh_config entry keeps the address "
                    f"{entry['address']} from an earlier run")
        else:
            what = f"it has no ssh_config entry, as {entry['why_none']}"
        if entry['keys']:
            what += ", and its ssh key was not added"
        notice = f"vm {entry['label']} is not running ({entry['state']}): {what}."
        if entry['state'] == 'not defined':
            return notice
        if entry['keys']:
            return (f"{notice} Once it runs, `boxman update` adds the key and "
                    f"refreshes the entry; `boxman up` brings it up and "
                    f"refreshes the entry, but adds no key.")
        return (f"{notice} `boxman up` brings it up and refreshes the entry; "
                f"`boxman update` refreshes it once it runs.")

    def generate_ssh_keys(self) -> bool:
        """
        Generate SSH keys for connecting to VMs.

        Creates an SSH key pair in the workspace directory if it doesn't
        already exist.

        A pair that cannot be made is a failure whatever the cluster: the
        ssh config names the private key as the IdentityFile, and only a
        host problem stops ssh-keygen here -- a missing binary, a directory
        that cannot be written, a full disk -- never a configuration.

        Returns:
            bool: True if successful, False otherwise
        """
        success = True
        ws_path = self.config.get('workspace', {}).get('path', '')

        for _, cluster in self._vm_clusters.items():
            base_path = os.path.expanduser(ws_path or cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')

            admin_priv_key = os.path.join(base_path, admin_key_name)
            admin_pub_key = os.path.join(base_path, f"{admin_key_name}.pub")

            # create directory if it doesn't exist -- the ssh config is
            # written there too. A directory that cannot be created used to
            # escape from here as a traceback.
            try:
                os.makedirs(base_path, exist_ok=True)
            except OSError as exc:
                self.logger.error(
                    f"cannot create {base_path} for the ssh key pair: "
                    f"{exc.strerror or exc}")
                success = False
                continue

            # generate key pair if it doesn't exist
            if not os.path.exists(admin_priv_key):
                self.logger.info(f"generating ssh key pair in {base_path}")

                try:
                    cmd = (f'ssh-keygen -t ed25519 -a 100 '
                           f'-f {shlex.quote(admin_priv_key)} -q -N ""')
                    run(cmd, hide=True, warn=True)

                    # verify keys were created
                    if os.path.isfile(admin_priv_key) and os.path.isfile(admin_pub_key):
                        self.logger.info(f"ssh key pair successfully generated at {admin_priv_key}")
                    else:
                        self.logger.error(f"failed to generate ssh key pair at {admin_priv_key}")
                        success = False

                except Exception as exc:
                    self.logger.error(f"error generating ssh key pair: {exc}")
                    success = False
            else:
                self.logger.info(f"using existing ssh key pair at {admin_priv_key}")

        return success

    def get_global_authorized_keys(self) -> list[str]:
        """
        Resolve and return all global SSH authorized keys from the app config.

        Reads ``ssh.authorized_keys`` from :pyattr:`app_config` (the top-level
        ``boxman.yml``), resolves each entry via :pyfunc:`fetch_value`, and
        returns a list of public-key strings.

        Returns:
            List of resolved SSH public key strings.
        """
        raw_keys = (
            (self.app_config or {})
            .get("ssh", {})
            .get("authorized_keys", [])
        )
        resolved: list[str] = []
        for entry in raw_keys:
            try:
                resolved.append(self.fetch_value(entry))
            except (ValueError, OSError) as exc:
                # OSError, not just FileNotFoundError: a file:// key that
                # exists but cannot be read is skipped the same way, rather
                # than escaping from the ssh setup as a traceback (#223)
                self.logger.warning(f"skipping unresolvable SSH key entry: {exc}")
        return resolved

    def write_global_authorized_keys_file(self, output_path: str) -> None:
        """
        Resolve global SSH keys from app_config and write them to a file.

        This bridges the Python-side boxman.yml config with the container
        entrypoint, which cannot read boxman.yml directly. The entrypoint
        reads ``global_authorized_keys`` from the bind-mounted ssh dir.

        Args:
            output_path: Path to write the authorized keys file
                         (e.g. ``<data_dir>/ssh/global_authorized_keys``).
        """
        keys = self.get_global_authorized_keys()
        if not keys:
            self.logger.info("no global authorized keys to write")
            return

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as fobj:
            for key in keys:
                fobj.write(key + "\n")
        self.logger.info(
            f"wrote {len(keys)} global authorized key(s) to {output_path}")

    @classmethod
    def fetch_value(cls, value) -> str:
        """
        Resolve a config reference (``${env:VAR}`` / ``file://…``) to its
        concrete value, or return the literal as-is.

        Thin wrapper around :func:`boxman.utils.references.resolve_reference`
        — kept on the class so external callers importing
        ``BoxmanManager.fetch_value`` keep working (Phase 2.4 extraction).

        Raises:
            ValueError: If the environment variable is not set.
            FileNotFoundError: If the referenced file does not exist.
        """
        return resolve_reference(value)

    def add_ssh_keys_to_vms(self) -> bool:
        """
        Add the generated SSH public key to all VMs to enable password-less login.

        Uses sshpass to add the public key to each VM using the admin password.
        Every VM is treated as running; :meth:`setup_ssh_access` passes
        :meth:`_push_ssh_keys` the VMs' states instead.

        Returns:
            bool: True if all VMs received the key successfully, False otherwise
        """
        return not self._push_ssh_keys()

    def _push_ssh_keys(self,
                       vm_states: dict[str, str] | None = None,
                       unreachable=()) -> list[str]:
        """
        Add the cluster admin key to every VM boxman expects to reach.

        A VM boxman expects to reach is running and not in *unreachable*:
        the VMs the verb did not wait for, because a network recreate could
        not reconnect them or provision could not start them. The others
        are skipped and are not failures -- write_ssh_config has already
        warned, once, about a VM that is not running.

        Every way this used to report "not all successful", classified
        (#223):

        - a VM that is not running: expected, not a failure;
        - an expected VM with no address, or whose key copy (or the ssh
          login that checks it) kept failing: a failure;
        - a cluster without an ``admin_pass``: a configuration boxman
          supports, not a failure. Nothing else authorizes the cluster key
          -- it is generated after the template, and no cloud-init or seed
          carries it -- so without a password boxman cannot add it, and
          says so once per cluster. The ISO-boot boxes run this way on
          purpose: the talos guest has no sshd, and the proxmox one
          authorizes its key from its answer file;
        - an ``admin_pass`` reference that cannot be resolved (an unset
          ``${env:...}``, a missing ``file://``), or a public key that is
          not there: the configuration asks for the key to be added and it
          cannot be, so it is one failure naming every expected VM of the
          cluster -- or a warning when none of them is running. The
          unresolved reference used to escape as a traceback.

        Args:
            vm_states: full VM name -> state, as for write_ssh_config. None
                treats every VM as running.
            unreachable: full names of the VMs not expected to answer.

        Returns:
            One line per problem, naming the VMs boxman expected to reach
            that did not get the key; empty when each one did.
        """
        failures: list[str] = []
        unreachable = set(unreachable)
        ws_path = self.config.get('workspace', {}).get('path', '')

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self._vm_clusters.items():
            base_path = os.path.expanduser(ws_path or cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')
            admin_pub_key = os.path.join(base_path, f"{admin_key_name}.pub")
            admin_user = cluster.get('admin_user', 'admin')

            targets = []
            left_out = []
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                label = f"{cluster_name}/{vm_name}"
                state = (None if vm_states is None
                         else vm_states.get(full_vm_name, 'not defined'))
                if state in self._NOT_RUNNING_STATES:
                    self.logger.info(
                        f"not adding the ssh key to vm {label}: it is not "
                        f"running ({state})")
                elif full_vm_name in unreachable:
                    left_out.append(label)
                else:
                    targets.append((vm_name, vm_info, full_vm_name, label))

            raw_pass = cluster.get('admin_pass')
            admin_pass = None
            problem = None
            if not _no_password(raw_pass):
                try:
                    admin_pass = self.fetch_value(raw_pass)
                except (ValueError, OSError) as exc:
                    # OSError, not just FileNotFoundError: a file:// that
                    # exists but cannot be read (mode 000, a directory on
                    # the way that cannot be searched) raises
                    # PermissionError and the like
                    problem = (f"the admin_pass of cluster {cluster_name} "
                               f"cannot be resolved ({exc})")
            if problem is None and _no_password(admin_pass):
                self.logger.warning(
                    f"cluster {cluster_name} has no admin_pass, so boxman "
                    f"adds no ssh key to its VMs: the ssh config reaches them "
                    f"only where the guest already authorizes {admin_pub_key}")
                continue
            if problem is None and not os.path.isfile(admin_pub_key):
                problem = f"the ssh public key {admin_pub_key} does not exist"
            for label in left_out:
                self.logger.warning(
                    f"not adding the ssh key to vm {label}: this run could "
                    f"not bring it up or reconnect it, see the errors above")
            if problem:
                if targets:
                    # one cause, one failure: it names every VM it stopped
                    labels = ', '.join(target[3] for target in targets)
                    self.logger.error(
                        f"cannot add the ssh key to {labels}: {problem}")
                    failures.append(f"ssh key not added to {labels}: {problem}")
                else:
                    self.logger.warning(
                        f"{problem}; no VM of cluster {cluster_name} is "
                        f"running, so no ssh key was due")
                continue

            if not targets:
                continue
            self.logger.info(f"adding ssh public key to VMs in cluster {cluster_name}")

            for vm_name, vm_info, full_vm_name, label in targets:
                # get the ip addresses for this vm
                ip_addresses = self.session_for_cluster(cluster_name).get_vm_ip_addresses(full_vm_name)

                if not ip_addresses:
                    self.logger.error(
                        f"vm {label} is running but has no ip address, "
                        f"cannot add its ssh key")
                    failures.append(
                        f"ssh key not added to {label}: it is running but "
                        f"has no ip address")
                    continue

                # use first available ip address
                ip_address = next(iter(ip_addresses.values()))

                self.logger.info(f"adding ssh key to vm {vm_name} ({ip_address})...")

                # try to add the key with exponential backoff
                hostname = hostname_or_key(vm_name, vm_info)
                prefixed_host = f"{cluster_name}_{hostname}"
                success = self._try_add_ssh_key(
                    ip_address=ip_address,
                    hostname=prefixed_host,
                    admin_user=admin_user,
                    admin_pass=admin_pass,
                    pub_key_path=admin_pub_key,
                    ssh_conf_path=os.path.join(
                        base_path, cluster.get('ssh_config', 'ssh_config'))
                )

                if success:
                    self.logger.info(f"successfully added the ssh key to the vm {vm_name}")
                else:
                    self.logger.error(f"failed to add the ssh key to the vm {vm_name}")
                    failures.append(
                        f"ssh key not added to {label} ({ip_address}): the "
                        f"key copy, or the ssh login that checks it, kept "
                        f"failing")

        return failures

    def _try_add_ssh_key(self,
                         ip_address: str,
                         hostname: str,
                         admin_user: str,
                         admin_pass: str,
                         pub_key_path: str,
                         ssh_conf_path: str) -> bool:
        """
        Try to add an SSH key to a VM with exponential backoff.

        Args:
            ip_address: IP address of the VM
            hostname: Hostname of the VM
            admin_user: Username for SSH login
            admin_pass: Password for SSH login
            pub_key_path: Path to the public key file
            ssh_conf_path: Path to the SSH config file

        Returns:
            bool: True if successful, False otherwise
        """
        wait_time = 1  # Start with 1 second
        max_retries = 10
        max_wait = 60  # Maximum wait per attempt

        for attempt in range(1, max_retries + 1):
            self.logger.info(
                f"attempt {attempt}/{max_retries} to add ssh key (waiting {wait_time}s)")

            # sshpass -e, not -p: an argv is world-readable in `ps` and
            # /proc for as long as the process lives, and this one runs up to
            # ten times per VM. -e takes the password from SSHPASS instead,
            # which narrows the audience to this process's own environment --
            # root and the docker daemon still see it (#164 CL-S2). Everything
            # else is quoted: a password with a space used to break auth, and
            # one with `$(...)` in it used to run.
            cmd = (
                f'sshpass -e ssh-copy-id -i {shlex.quote(pub_key_path)} '
                f'-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
                f'{shlex.quote(f"{admin_user}@{ip_address}")}'
            )

            # When using a non-local runtime, the VM is only reachable from
            # inside the container, so wrap the command with docker exec --
            # which also has to carry SSHPASS across, since a container does
            # not inherit this process's environment.
            cmd = self.runtime_instance.wrap_command(cmd, pass_env=('SSHPASS',))

            # env= adds to the environment rather than replacing it, so the
            # rest of it (PATH, DOCKER_HOST, ...) still reaches the command.
            # str(): an all-digit password in an unquoted Jinja value comes
            # back from yaml.safe_load as an int, and resolve_reference keeps
            # non-strings as they are. The f-string this replaced converted it
            # on the way past; subprocess rejects it outright.
            result = run(cmd, hide=True, warn=True,
                         env={'SSHPASS': str(admin_pass)})

            if result.ok:
                # Log ssh-copy-id informational output
                if result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        self.logger.info(f"ssh-copy-id: {line}")

                # verify we can ssh without password
                ssh_success = self._verify_ssh_connection(hostname, ssh_conf_path)

                if ssh_success:
                    return True
            else:
                # Log ssh-copy-id output — warning for retries, error on last attempt
                is_last = attempt == max_retries
                log_fn = self.logger.error if is_last else self.logger.warning
                combined = (result.stderr.strip() or result.stdout.strip())
                if combined:
                    for line in combined.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        log_fn(f"ssh-copy-id: {line}")
                else:
                    log_fn("ssh key addition failed (no output)")

            # wait before next attempt with exponential backoff
            time.sleep(wait_time)
            wait_time = min(wait_time * 2, max_wait)

        return False

    def _verify_ssh_connection(self, hostname: str, ssh_config_path: str) -> bool:
        """
        Verify SSH connection to a VM.

        Args:
            hostname: Hostname of the VM
            ssh_config_path: Path to the SSH config file

        Returns:
            bool: True if successful, False otherwise
        """
        self.logger.info(f"verifying ssh connection to: {hostname}")
        # quoted for the shell: a blank in the workspace split the -F path,
        # so every key check failed and the push was reported as failed
        target = f'-F {shlex.quote(ssh_config_path)} {shlex.quote(hostname)}'
        ssh_cmd = f'ssh -o BatchMode=yes {target} hostname'

        # When using a non-local runtime, the VM is only reachable from
        # inside the container. The shared ssh_config contains a
        # ProxyJump directive aimed at the host-side published SSH port
        # (e.g. 127.0.0.1:2417), which is *not* reachable from inside
        # the container's own netns — there, 127.0.0.1:2417 is a dead
        # port. Override ProxyJump/ProxyCommand to "none" so the
        # in-container ssh hits the VM IP directly (via the `Hostname`
        # directive in the VM's Host block).
        if self._runtime_name != 'local':
            ssh_cmd = (
                f'ssh -o BatchMode=yes -o ProxyJump=none '
                f'-o ProxyCommand=none {target} hostname'
            )
        ssh_cmd = self.runtime_instance.wrap_command(ssh_cmd)

        result = run(ssh_cmd, hide=True, warn=True)

        if result.ok and result.stdout.strip():
            hostname_output = result.stdout.strip()
            self.logger.info(f"ssh connection verified: {hostname_output}")
            return True
        else:
            self.logger.error(f"ssh connection failed for {hostname}: {result.stderr.strip()}")
            return False

    def setup_ssh_access(self, fresh=(), unreachable=()) -> bool:
        """
        Set up SSH access to all VMs.

        This method:
        1. Writes global authorized keys file (resolved from boxman.yml)
        2. Generates SSH keys if they don't exist
        3. Writes an SSH config file for easy access, in which a VM that
           is not running keeps its earlier address
        4. Adds the public key to every VM boxman expects to reach

        Each step runs whatever the one before it managed, and a problem is
        raised only at the end: it used to be returned as False, which both
        callers dropped, so `update` logged "failed to add ssh keys to some
        vms" and exited 0 (#223). The VMs' states are read once, for steps
        3 and 4 both.

        Args:
            fresh: full names of the VMs created in this run; see
                :meth:`write_ssh_config`.
            unreachable: full names of the VMs the caller did not wait for
                (provision's VMs that never started). With the ones a
                network recreate could not reconnect, they are not expected
                to answer, and not getting the key is not a failure for
                them.

        Returns:
            bool: True -- a problem raises instead.

        Raises:
            SSHAccessError: after all four steps, naming each problem -- a
                global authorized keys file that could not be written, a key
                pair that could not be generated, an ssh config that could
                not be written, a VM boxman expected to reach that did not
                get the key.
        """
        failures: list[str] = []

        # write global authorized keys so they can be consumed by container
        # entrypoints or cloud-init scripts. A cluster whose file cannot be
        # written -- a file where its workdir should be, a directory it may
        # not write -- is recorded like any other step's problem: it used to
        # escape as a traceback before anything below had run.
        for cluster_name, cluster in self._vm_clusters.items():
            workdir = os.path.expanduser(cluster['workdir'])
            global_keys_path = os.path.join(workdir, 'global_authorized_keys')
            try:
                self.write_global_authorized_keys_file(global_keys_path)
            except OSError as exc:
                reason = exc.strerror or str(exc)
                self.logger.error(
                    f"could not write the global authorized keys of cluster "
                    f"{cluster_name} to {global_keys_path}: {reason}")
                failures.append(
                    f"the global authorized keys of cluster {cluster_name} "
                    f"could not be written to {global_keys_path}: {reason}")

        if not self.generate_ssh_keys():
            self.logger.error("failed to generate ssh keys")
            failures.append(
                "the admin ssh key pair could not be generated, see the "
                "errors above")

        vm_states = self._vm_states_if_known()
        try:
            self.write_ssh_config(
                vm_states=vm_states, fresh=fresh, adding_keys=True)
        except SSHAccessError as exc:
            failures.extend(exc.failures)

        unreachable = (set(unreachable)
                       | set(getattr(self, '_reattach_failed_vms', ())))
        failures.extend(self._push_ssh_keys(
            vm_states=vm_states, unreachable=unreachable))

        if failures:
            raise SSHAccessError(failures)

        self.logger.info("")
        self.logger.info("ssh access setup complete")
        self.logger.info("you can now connect to vms using the ssh config file")

        return True
