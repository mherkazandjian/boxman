#!/usr/bin/env python

import argparse
import logging
import os
import shutil
import sys
from argparse import RawTextHelpFormatter
from datetime import datetime, timezone
from multiprocessing import Process

import yaml

import boxman
from boxman import log
from boxman.abstract.providers import ProviderSession as Session
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.import_image import ImageImporter
from boxman.providers.libvirt.session import LibVirtSession
from boxman.scripts.cli_parser import parse_args
from boxman.utils.jinja_env import create_jinja_env
from boxman.virtualbox.vboxmanage import Virtualbox

now = datetime.now(timezone.utc)
snap_name = now.strftime('%Y-%m-%dT%H:%M:%S')


def parse_vms_list(session: Session, cli_args):
    """
    Parse the list of vms, either "all" vms or a comma-separated list

    When 'all' is specified, all the vms from the session configurations
    are used.

    :param session: The instance of a session
    """
    vms = []
    if cli_args.vms == 'all':
        for cluster in session.conf['clusters']:
            for vm in session.conf['clusters'][cluster]['vms']:
                vms.append(vm)
    else:
        vms.extend(cli_args.vms.split(','))

    return vms


def snapshot_take(session, cli_args):
    """
    Take a snapshot of the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli
    """
    vms = parse_vms_list(session, cli_args)
    def _take(vm):
        session.snapshot.take(
            vm,
            snap_name=cli_args.snapshot_name,
            live=cli_args.live)
    processes = [Process(target=_take, args=(vm,)) for vm in vms]
    [p.start() for p in processes]
    [p.join() for p in processes]
    #_ = [_take(vm) for vm in vms]


def snapshot_list(session, cli_args):
    vms = parse_vms_list(session, cli_args)
    for vm in vms:
        session.snapshot.list(vm)


def snapshot_delete(session, cli_args):
    vms = parse_vms_list(session, cli_args)
    for vm in vms:
        session.snapshot.delete(
            vm,
            snap_name=cli_args.snapshot_name)


def snapshot_restore(session, cli_args):
    """
    Restore a snapshot of the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli
    """
    vms = parse_vms_list(session, cli_args)
    def _restore(vm):
        session.snapshot.restore(
            vm,
            snap_name=cli_args.snapshot_name)
    processes = [Process(target=_restore, args=(vm,)) for vm in vms]
    [p.start() for p in processes]
    [p.join() for p in processes]


def machine_suspend(session, cli_args):
    """
    Suspend the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli
    """
    vms = parse_vms_list(session, cli_args)
    def _suspend(vm):
        session.suspend(vm)
    processes = [Process(target=_suspend, args=(vm,)) for vm in vms]
    [p.start() for p in processes]
    [p.join() for p in processes]


def machine_resume(session, cli_args):
    """
    Resume the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli
    """
    vms = parse_vms_list(session, cli_args)
    def _resume(vm):
        session.resume(vm)
    processes = [Process(target=_resume, args=(vm,)) for vm in vms]
    [p.start() for p in processes]
    [p.join() for p in processes]


def machine_save(session, cli_args):
    """
    Save the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli
    """
    vms = parse_vms_list(session, cli_args)
    def _save(vm):
        session.savestate(vm)
    processes = [Process(target=_save, args=(vm,)) for vm in vms]
    [p.start() for p in processes]
    [p.join() for p in processes]


def machine_start(session, cli_args):
    """
    Start the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli
    """
    vms = parse_vms_list(session, cli_args)
    def _start(vm):
        session.startvm(vm)
    processes = [Process(target=_start, args=(vm,)) for vm in vms]
    [p.start() for p in processes]
    [p.join() for p in processes]


def export_config(session: Session, cli_args):
    """
    Take the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli

    .. todo:: add option to specify exporting a certain snapshot
    """
    vms = parse_vms_list(session, cli_args)
    assert cli_args.path
    os.makedirs(cli_args.path, exist_ok=False)
    def _export_vm(vm, dirpath):
        session.savestate(vm)
        session.export_vm(
            vm,
            path=os.path.expanduser(os.path.join(dirpath, f'{vm}.ovf'))
        )
        session.startvm(vm)
    processes = [Process(target=_export_vm, args=(vm, cli_args.path)) for vm in vms]
    [p.start() for p in processes]
    [p.join() for p in processes]
    #_ = [_export_vm(vm, cli_args.export_path) for vm in vms]


def import_config(session: Session, cli_args):
    """
    Take a snapshot of the vms

    :param session: The instance of a session
    :param cli_args: The parsed arguments from the cli
    # .. todo:: add option to start/resume the vms once imported
    """
    raise NotImplementedError('importing is not implemented yet')
    #vms = parse_vms_list(session, cli_args)
    #def _take(vm):
    #    session.snapshot.take(
    #        vm,
    #        snap_name=cli_args.snapshot_name,
    #        live=cli_args.live)
    #processes = [Process(target=_take, args=(vm,)) for vm in vms]
    #[p.start() for p in processes]
    #[p.join() for p in processes]
    ##_ = [_take(vm) for vm in vms]

def _default_boxman_config() -> dict:
    """
    Return the default boxman application configuration.

    Uses system paths for virt-install, virt-clone, and virsh (resolved
    via ``shutil.which``), with verbose and use_sudo both set to False.
    """
    return {
        "runtime": "local",
        "runtime_config": {
            "runtime_container": "boxman-libvirt-default",
        },
        "ssh": {
            "authorized_keys": [],
        },
        "cache": {
            "enabled": True,
            "cache_dir": "~/.cache/boxman/images",
        },
        "providers": {
            "libvirt": {
                "uri": "qemu:///system",
                "use_sudo": False,
                "verbose": False,
                "virt_install_cmd": shutil.which("virt-install") or "virt-install",
                "virt_clone_cmd": shutil.which("virt-clone") or "virt-clone",
                "virsh_cmd": shutil.which("virsh") or "virsh",
            },
        },
    }


def load_boxman_config(path: str) -> dict:
    """
    Load the boxman configuration from the specified path.

    The file is rendered as a Jinja2 template (supporting ``{{ env() }}``,
    ``{{ env_required() }}``, ``{{ env_is_set() }}``) before being parsed
    as YAML.

    If *path* points to the default location
    (``~/.config/boxman/boxman.yml``) and the file does not exist, a new
    file is created with sensible defaults (system paths for libvirt
    tools, ``verbose: False``, ``use_sudo: False``).

    For any other path a :class:`FileNotFoundError` is raised when the
    file is missing.

    :param path: The path to the configuration file
    :return: The configuration dictionary
    """
    expanded = os.path.expanduser(path)
    default_path = os.path.expanduser("~/.config/boxman/boxman.yml")

    if not os.path.isfile(expanded):
        # Only auto-create when using the default location
        if os.path.abspath(expanded) == os.path.abspath(default_path):
            os.makedirs(os.path.dirname(default_path), exist_ok=True)
            config = _default_boxman_config()
            with open(default_path, "w") as fobj:
                yaml.dump(config, fobj, default_flow_style=False)
            log.info(
                f"created default boxman config at {default_path}"
            )
            return config
        else:
            raise FileNotFoundError(
                f"boxman config not found: {expanded}"
            )

    # Render as Jinja2 template to resolve {{ env() }} etc.
    config_dir = os.path.dirname(os.path.abspath(expanded))
    config_filename = os.path.basename(expanded)

    jinja_env = create_jinja_env(config_dir)
    template = jinja_env.get_template(config_filename)
    rendered = template.render(environ=os.environ)

    config = yaml.safe_load(rendered)
    return config


def main():

    arg_parser = parse_args()
    args, remaining = arg_parser.parse_known_args()

    # parse_known_args may leave '--' and trailing positional args in
    # *remaining* when unknown flags appear before '--'.  Split them
    # back out so that extra_args is filled correctly.
    if "--" in remaining:
        sep_idx = remaining.index("--")
        extra_after = remaining[sep_idx + 1:]
        remaining = remaining[:sep_idx]
        if hasattr(args, "extra_args"):
            args.extra_args = (args.extra_args or []) + extra_after

    # Only the 'run' subcommand accepts dynamic task flags;
    # all other subcommands should reject unknown arguments.
    if remaining and (
        not hasattr(args, "func") or args.func != BoxmanManager.run_task
    ):
        arg_parser.error(f"unrecognized arguments: {' '.join(remaining)}")

    args.remaining_args = remaining

    if args.version:
        print(f'v{boxman.metadata.version}')
        sys.exit(0)

    if not hasattr(args, 'func'):
        arg_parser.print_help()
        sys.exit(1)

    if args.func == BoxmanManager.list_projects:
        manager = BoxmanManager(config=None)
        args.func(manager, args)
        sys.exit(0)

    # Handle 'image push' — provider-agnostic; no project config needed.
    if args.func == BoxmanManager.push_image:
        args.func(None, args)
        sys.exit(0)

    # Handle 'ps' — needs config and virsh but not a full provider session
    if args.func == BoxmanManager.ps:
        _boxman_logger = logging.getLogger('boxman')
        _ps_json = getattr(args, 'json', False)
        if _ps_json:
            _boxman_logger.setLevel(logging.CRITICAL + 1)
        try:
            manager = BoxmanManager(config=args.conf)
            if not manager.config:
                _boxman_logger.setLevel(logging.DEBUG)
                log.error("no project config found (conf.yml)")
                sys.exit(1)
            args.func(manager, args)
        finally:
            _boxman_logger.setLevel(logging.DEBUG)
        sys.exit(0)

    # Handle 'conf' — show effective merged configuration
    if args.func == BoxmanManager.show_conf:
        manager = BoxmanManager(config=args.conf)
        if not manager.config:
            log.error("no project config found (conf.yml)")
            sys.exit(1)
        boxman_config = load_boxman_config(os.path.expanduser(args.boxman_conf))
        manager.load_app_config(boxman_config)
        runtime = args.runtime or boxman_config.get('runtime', 'local')
        manager.runtime = runtime
        # Compute merged provider config (same logic as provision path)
        provider_type = (
            list(manager.config.get('provider', {}).keys())[0]
            if 'provider' in manager.config else 'libvirt'
        )
        provider_conf_with_runtime = manager.get_provider_config_with_runtime(
            boxman_config.get('providers', {}).get(provider_type, {})
        )
        project_provider = manager.config.get('provider', {}).get(provider_type, {})
        merged_provider = provider_conf_with_runtime.copy()
        merged_provider.update(project_provider)
        args.func(manager, args, merged_provider=merged_provider)
        sys.exit(0)

    # Handle 'run' — needs config but not a provider session or runtime
    if args.func == BoxmanManager.run_task:
        manager = BoxmanManager(config=args.conf)
        if not manager.config:
            log.error("no project config found (conf.yml)")
            sys.exit(1)
        if not manager.config.get("tasks") and not getattr(args, "cmd", None):
            if not getattr(args, "list_tasks", False):
                log.error(
                    "no 'tasks' section found in conf.yml. "
                    "Define tasks or use --cmd for ad-hoc commands."
                )
                sys.exit(1)
        args.func(manager, args)
        sys.exit(0)

    # Handle 'ssh' — needs config but not a provider session or runtime
    if args.func == BoxmanManager.ssh_session:
        manager = BoxmanManager(config=args.conf)
        if not manager.config:
            log.error("no project config found (conf.yml)")
            sys.exit(1)
        args.func(manager, args)
        sys.exit(0)

    else:
        # use the config of a deployment specified on the cmd line only if
        # not importing an image
        config = None if args.func == BoxmanManager.import_image else args.conf
        manager = BoxmanManager(config=config)

        # load the boxman app configuration
        boxman_config = load_boxman_config(os.path.expanduser(args.boxman_conf))

        # make the app-level (boxman.yml) available to the manager
        manager.load_app_config(boxman_config)

        # resolve the runtime: CLI flag overrides boxman.yml default
        runtime = args.runtime or boxman_config.get('runtime', 'local')
        manager.runtime = runtime

        # If any workdir on disk was previously owned by a different
        # runtime, prompt the user to switch to a runtime-specific path
        # before we lock it into the bind-mount list. Skip for destroy
        # (we're about to nuke the workdir anyway — prompting would be
        # absurd, especially with -y).
        if args.func != BoxmanManager.destroy:
            manager.reconcile_workdirs_with_runtime(
                manager.runtime_instance.name)

        # tell the runtime where the project conf.yml lives so bundled
        # assets are deployed next to it (in .boxman/runtime/docker/)
        if manager.runtime_instance.name == 'docker-compose':
            conf_dir = os.path.abspath(os.path.dirname(args.conf))
            manager.runtime_instance.project_dir = conf_dir

            # Set the project name on the runtime so Docker resources
            # (container, volumes, network) are scoped per project.
            if manager.config and 'project' in manager.config:
                manager.runtime_instance.project_name = manager.config['project']

            # Collect every workdir from the project config (clusters and
            # templates) and pass them to the runtime so they can be
            # bind-mounted into the container. Template workdirs must be
            # included — otherwise qemu-img/rsync inside the container can
            # not see files copied to the host-side template directory.
            workdirs = manager.collect_workdirs()
            if workdirs:
                manager.runtime_instance.workdirs = workdirs
                # Pre-create each bind-mount dir on the host AS THE
                # CURRENT USER. Without this, `docker compose up` would
                # create the missing host directory (as root) when it
                # sets up the bind mount, and subsequent host-side
                # file writes (env.sh, ssh_config, …) would hit
                # PermissionError. If the dir already exists as root
                # from an earlier failed run, _ensure_writable_dir fixes
                # ownership via `sudo chown`.
                for wd in workdirs:
                    log.info(f"runtime workdir: {wd}")
                    try:
                        manager._ensure_writable_dir(wd)
                    except Exception as exc:
                        log.warning(f"could not prepare {wd}: {exc}")

        # Handle destroy-runtime — tear down Docker resources without
        # starting the container first
        if args.func == BoxmanManager.destroy_runtime:
            args.func(manager, args)
            sys.exit(0)

        # Commands that manage the runtime themselves (they start it
        # best-effort rather than hard-requiring it, so they still work
        # when the runtime is broken or unreachable).
        manages_own_runtime = args.func == BoxmanManager.destroy

        if not manages_own_runtime:
            # ensure the runtime environment is up and ready before proceeding
            manager.runtime_instance.ensure_ready()

        # Handle create-templates — it doesn't need a full provider session
        if args.func == BoxmanManager.create_templates:
            args.func(manager, args)
            sys.exit(0)

        if args.func == BoxmanManager.import_image:

            # if the provider is specified in the cmd line, use it
            if args.provider:
                provider_type = args.provider
            else:
                # Fetch the manifest (file:// or http(s)://) once to discover
                # the provider type. The downloaded path is reused below to
                # avoid re-fetching the manifest in the provider session.
                try:
                    manifest, manifest_local_path = ImageImporter.load_manifest_from_uri(
                        args.manifest_uri)
                except ValueError as exc:
                    log.error(str(exc))
                    sys.exit(2)
                provider_type = manifest['provider']
                # Stash the resolved local path so the session reuses it.
                args.manifest_local_path = manifest_local_path

            # fetch the provider configuration from the boxman config
            manager.config = boxman_config['providers'][provider_type]
        else:
            provider_type = list(manager.config['provider'].keys())[0]

        if provider_type == 'virtualbox':
            session = Virtualbox(manager.config)    # .. todo:: rename to VirtualBoxSession
        elif provider_type == 'libvirt':
            # merge runtime metadata into the provider config from boxman.yml
            provider_conf_with_runtime = manager.get_provider_config_with_runtime(
                boxman_config.get('providers', {}).get(provider_type, {})
            )
            # enrich the project config with runtime-aware provider settings
            # App-level (boxman.yml) settings serve as DEFAULTS;
            # project-level (conf.yml) settings always take precedence.
            enriched_config = manager.config.copy()
            if 'provider' in enriched_config and provider_type in enriched_config['provider']:
                project_provider = enriched_config['provider'][provider_type].copy()
                # Start from app-level defaults, then overlay project-level on top
                merged_provider = provider_conf_with_runtime.copy()
                merged_provider.update(project_provider)
                enriched_config['provider'][provider_type] = merged_provider

            session = LibVirtSession(enriched_config)
            session.manager = manager
            manager.provider = session
        elif provider_type == 'docker-compose':
            raise NotImplementedError('docker-compose is not implemented yet')

        args.func(manager, args)


if __name__ == '__main__':
    main()
