#!/usr/bin/env python

import logging
import os
import shutil
import sys

import yaml

import boxman
from boxman import log
from boxman.exceptions import BoxmanError, ConfigError
from boxman.loggers.logger import set_quiet, set_verbosity, suppressed
from boxman.manager import BoxmanManager
from boxman.providers import create_session, merge_provider_configs, primary_provider_type
from boxman.providers.libvirt.import_image import ImageImporter
from boxman.scripts.cli_parser import parse_args, resolve_verbosity
from boxman.utils.jinja_env import create_jinja_env

#: Names of the :class:`BoxmanManager` methods the CLI may dispatch to.
#: The parser declares them via ``set_defaults(handler='<name>')`` and
#: :func:`_main` resolves the bound method with ``getattr`` at dispatch
#: time, so decorating or aliasing a handler method can never silently
#: break dispatch (the old ``args.func is BoxmanManager.x`` identity
#: comparisons did).
_CLI_HANDLERS = frozenset({
    'import_image', 'push_image', 'inspect_image', 'create_templates',
    'list_projects', 'provision', 'up', 'update', 'down',
    'destroy_runtime', 'destroy', 'deprovision', 'snapshot_take',
    'snapshot_list', 'snapshot_log', 'snapshot_restore', 'snapshot_delete',
    'snapshot_collapse', 'storage_df', 'storage_trim', 'storage_compact',
    'storage_optimize', 'storage_compress_snapshots', 'suspend_vm',
    'resume_vm', 'save_vm', 'start_vm', 'run_task', 'ps', 'show_conf',
    'ssh_session', 'exec_container', 'pxe_boot', 'netlab_deploy',
    'netlab_destroy', 'netlab_inspect', 'netlab_ssh',
})


def ensure_virtualbox_runtime_is_local(runtime: str) -> None:
    """
    Guard: the VirtualBox provider only supports the ``local`` runtime.

    VirtualBox is a host-local hypervisor — ``VBoxManage`` must run directly on
    the host and cannot be driven through a container runtime. Any non-``local``
    runtime is therefore a configuration error.

    Args:
        runtime: The resolved runtime name (e.g. ``local``, ``docker``).

    Raises:
        ConfigError: If *runtime* is anything other than ``local``.
    """
    if runtime != 'local':
        raise ConfigError(
            f"the 'virtualbox' provider only supports the 'local' runtime, "
            f"but the resolved runtime is '{runtime}'. Remove the --runtime "
            f"flag / the 'runtime:' setting in boxman.yml, or set it to 'local'."
        )


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
    """
    CLI entry point.

    Thin wrapper that translates any :class:`~boxman.exceptions.BoxmanError`
    into a clean ``log.error`` + exit 2 instead of surfacing a traceback.
    This covers config-schema errors (:class:`~boxman.exceptions.ConfigError`,
    e.g. an unsupported ``version:`` or an invalid v2.0 cluster) as well as
    operational failures on the docker-compose path — a service that never
    becomes healthy within ``readiness_timeout``
    (:class:`~boxman.exceptions.ProvisionError`) or a missing docker/compose
    plugin (:class:`~boxman.exceptions.RuntimeUnavailable`). All other flow
    (including the ``sys.exit()`` calls throughout) lives in :func:`_main`.

    Also translates the virtualbox provider's ``NotImplementedError`` stubs
    into a clean exit 2: the provider is registered but Phase 1 (config
    surface only, non-functional), so without this every operation would die
    with a raw traceback mid-flow.
    """
    try:
        _main()
    except BoxmanError as exc:
        # A viewer command (e.g. ``snapshot log``) raises the boxman logger
        # to CRITICAL+1 to silence INFO spam before the config is loaded; if
        # loading then fails, restore a level that lets this error through so
        # it is not swallowed (exit 2 with empty output).
        logging.getLogger('boxman').setLevel(logging.ERROR)
        log.error(str(exc))
        sys.exit(2)
    except NotImplementedError as exc:
        if not str(exc).startswith("VirtualBox provider:"):
            raise
        # The virtualbox provider is registered but Phase 1 (config surface
        # only, non-functional): every operation stub raises
        # NotImplementedError. Translate it into a clear message instead of
        # a raw traceback mid-flow.
        logging.getLogger('boxman').setLevel(logging.ERROR)
        log.error(
            f"{exc} — the virtualbox provider is Phase 1 "
            "(config surface only, non-functional)")
        sys.exit(2)


def _main():

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
        getattr(args, 'handler', None) != 'run_task'
    ):
        arg_parser.error(f"unrecognized arguments: {' '.join(remaining)}")

    args.remaining_args = remaining

    # Verbosity: minimal (STATUS) by default; -v/-vv/-vvv (either position)
    # reveal more, -q/--quiet shows warnings+errors only. This overrides the
    # import-time default in loggers/logger.py.
    _verbosity = 0
    if getattr(args, 'quiet', 0):
        set_quiet()
    else:
        _verbosity = resolve_verbosity(args)
        set_verbosity(_verbosity)

    if args.version:
        print(f'v{boxman.metadata.version}')
        sys.exit(0)

    if not hasattr(args, 'handler'):
        arg_parser.print_help()
        sys.exit(1)

    # Defensive allowlist: the parser only ever sets these names, but
    # dispatch resolves them with getattr — reject anything unexpected
    # instead of risking an attribute lookup on the manager.
    if args.handler not in _CLI_HANDLERS:
        arg_parser.error(f"unknown command handler: {args.handler}")

    if args.handler == 'list_projects':
        manager = BoxmanManager(config=None)
        getattr(manager, args.handler)(args)
        sys.exit(0)

    # Handle 'image push' — provider-agnostic; no project config needed.
    if args.handler == 'push_image':
        manager = BoxmanManager(config=None)
        getattr(manager, args.handler)(args)
        sys.exit(0)

    # Handle 'image inspect' — provider-agnostic; no project config needed.
    if args.handler == 'inspect_image':
        manager = BoxmanManager(config=None)
        getattr(manager, args.handler)(args)
        sys.exit(0)

    # Handle 'ps' — needs config and virsh but not a full provider session
    if args.handler == 'ps':
        import contextlib
        _ps_json = getattr(args, 'json', False)
        _cm = suppressed() if _ps_json else contextlib.nullcontext()
        with _cm:
            manager = BoxmanManager(config=args.conf)
            if not manager.config:
                log.error("no project config found (conf.yml)")
                sys.exit(1)
            # same load/merge/inject path as the other verbs so ps honors
            # boxman.yml (uri, sudo lists) and the resolved runtime
            boxman_config = load_boxman_config(os.path.expanduser(args.boxman_conf))
            manager.load_app_config(boxman_config)
            manager.runtime = args.runtime or boxman_config.get('runtime', 'local')
            # scope the runtime container to this project (as the full
            # session path does) so ps targets the right container
            if manager.runtime_instance.name == 'docker-compose':
                manager.runtime_instance.project_dir = os.path.abspath(
                    os.path.dirname(args.conf))
                if 'project' in manager.config:
                    manager.runtime_instance.project_name = manager.config['project']
            getattr(manager, args.handler)(args)
        sys.exit(0)

    # 'snapshot log' is a viewer command — suppress INFO logging across the
    # whole run so only the rendered tree (or JSON) reaches the user. virsh
    # execute spam from per-VM snapshot-dumpxml/info/current calls would
    # otherwise drown the few lines of actual output. The dispatch then
    # falls through to the regular provider-setup path below.
    if args.handler == 'snapshot_log':
        logging.getLogger('boxman').setLevel(logging.CRITICAL + 1)

    # Handle 'conf' — show effective merged configuration
    if args.handler == 'show_conf':
        manager = BoxmanManager(config=args.conf)
        if not manager.config:
            log.error("no project config found (conf.yml)")
            sys.exit(1)
        boxman_config = load_boxman_config(os.path.expanduser(args.boxman_conf))
        manager.load_app_config(boxman_config)
        runtime = args.runtime or boxman_config.get('runtime', 'local')
        manager.runtime = runtime
        # Compute merged provider config (same logic as provision path)
        provider_type = primary_provider_type(manager.config)
        provider_conf_with_runtime = manager.get_provider_config_with_runtime(
            boxman_config.get('providers', {}).get(provider_type, {})
        )
        project_provider = manager.config.get('provider', {}).get(provider_type, {})
        merged_provider = merge_provider_configs(
            provider_conf_with_runtime, project_provider)
        getattr(manager, args.handler)(args, merged_provider=merged_provider)
        sys.exit(0)

    # Handle 'run' — needs config but not a provider session or runtime
    if args.handler == 'run_task':
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
        getattr(manager, args.handler)(args)
        sys.exit(0)

    # Handle 'ssh' — needs config but not a provider session or runtime
    if args.handler == 'ssh_session':
        manager = BoxmanManager(config=args.conf)
        if not manager.config:
            log.error("no project config found (conf.yml)")
            sys.exit(1)
        getattr(manager, args.handler)(args)
        sys.exit(0)

    # Handle 'exec' — container access; needs config but not the full libvirt
    # provider setup (the dc session is created on demand via _dc_session).
    if args.handler == 'exec_container':
        manager = BoxmanManager(config=args.conf)
        if not manager.config:
            log.error("no project config found (conf.yml)")
            sys.exit(1)
        getattr(manager, args.handler)(args)
        sys.exit(0)

    else:
        # use the config of a deployment specified on the cmd line only if
        # not importing an image
        config = None if args.handler == 'import_image' else args.conf
        manager = BoxmanManager(config=config)

        # load the boxman app configuration
        boxman_config = load_boxman_config(os.path.expanduser(args.boxman_conf))

        # -vvv: echo the underlying shell commands (provider 'verbose' flag).
        if _verbosity >= 3:
            _providers = boxman_config.setdefault('providers', {})
            for _pname, _pcfg in list(_providers.items()):
                if isinstance(_pcfg, dict):
                    _pcfg['verbose'] = True

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
        if args.handler != 'destroy':
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
        if args.handler == 'destroy_runtime':
            getattr(manager, args.handler)(args)
            sys.exit(0)

        # Commands that manage the runtime themselves (they start it
        # best-effort rather than hard-requiring it, so they still work
        # when the runtime is broken or unreachable).
        manages_own_runtime = args.handler == 'destroy'

        if not manages_own_runtime:
            # ensure the runtime environment is up and ready before proceeding
            manager.runtime_instance.ensure_ready()

        # Handle create-templates — it doesn't need a full provider session
        if args.handler == 'create_templates':
            getattr(manager, args.handler)(args)
            sys.exit(0)

        if args.handler == 'import_image':

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
            provider_type = primary_provider_type(manager.config)

        # Build a session for every provider declared in the project config
        # via the registry (import-image keeps its single-provider flow —
        # the provider type comes from the manifest / CLI flag above).
        if args.handler == 'import_image':
            provider_types = [provider_type]
        else:
            provider_types = (
                list(manager.config.get('provider', {}).keys()) or [provider_type]
            )
            # Also cover any cluster-level ``provider:`` override so a mixed
            # config fails fast with the friendly registry error (exit 2)
            # instead of a mid-provision traceback when a cluster resolves
            # to a provider no session was built for.
            for _cluster_name in (manager.config.get('clusters') or {}):
                _cluster_type = manager.provider_type_for_cluster(_cluster_name)
                if _cluster_type not in provider_types:
                    provider_types.append(_cluster_type)

        for _ptype in provider_types:
            # The docker-compose PROVIDER shells out to `docker compose` on
            # the host; the `docker-compose` RUNTIME is libvirt-in-a-container
            # (a different axis). Requiring runtime 'local' keeps them from
            # being confused. Fail fast with a clean message.
            if _ptype == 'docker-compose' and manager.runtime != 'local':
                log.error(
                    f"the docker-compose provider requires runtime 'local' "
                    f"(got '{manager.runtime}'). The 'docker-compose' runtime "
                    f"is libvirt-in-a-container — a different setting; see "
                    f"doc/docker-compose-provider/config-schema.md."
                )
                sys.exit(2)
            if _ptype == 'virtualbox':
                # VirtualBox is a host-local hypervisor: fail fast before
                # building the session if the resolved runtime is not 'local'.
                try:
                    ensure_virtualbox_runtime_is_local(manager.runtime)
                except ConfigError as exc:
                    log.error(str(exc))
                    sys.exit(2)
            # merge runtime metadata into the provider config from boxman.yml
            provider_conf_with_runtime = manager.get_provider_config_with_runtime(
                boxman_config.get('providers', {}).get(_ptype, {})
            )
            # Enrich the project config with runtime-aware provider
            # settings — for EVERY provider type, so app-level
            # (boxman.yml) settings serve as DEFAULTS and project-level
            # (conf.yml) settings always take precedence. The provider
            # block is built unconditionally so a project without a
            # ``provider:`` section (defaulted to libvirt by
            # primary_provider_type) still inherits the runtime URI/sudo
            # defaults instead of silently falling back to a local
            # qemu:///system.
            enriched_config = manager.config.copy()
            existing_provider = enriched_config.get('provider') or {}
            project_provider = (existing_provider.get(_ptype) or {}).copy()
            # Start from app-level defaults, then overlay project-level on
            # top via the shared sudo-list-aware merge
            merged_provider = merge_provider_configs(
                provider_conf_with_runtime, project_provider)
            enriched_config['provider'] = {
                **existing_provider,
                _ptype: merged_provider,
            }
            session_config = enriched_config

            try:
                session = create_session(_ptype, session_config)
            except (NotImplementedError, ValueError) as exc:
                log.error(str(exc))
                sys.exit(2)
            session.manager = manager
            manager.register_session(_ptype, session)

        getattr(manager, args.handler)(args)


if __name__ == '__main__':
    main()
