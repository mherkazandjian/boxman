#!/usr/bin/env python

import os
import sys
import argparse
from argparse import RawTextHelpFormatter
from datetime import datetime, timezone
from multiprocess import Process
import yaml
import json
import shutil

import boxman
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.session import LibVirtSession
from boxman.virtualbox.vboxmanage import Virtualbox
from boxman.utils.io import write_files
#from boxman.abstract.providers import Providers
from boxman.abstract.providers import ProviderSession as Session
from boxman import log
from boxman.utils.jinja_env import create_jinja_env


now = datetime.now(timezone.utc)
snap_name = now.strftime('%Y-%m-%dT%H:%M:%S')


def parse_args():

    parser = argparse.ArgumentParser(
        description=(
            f"Boxman version {boxman.metadata.version}\n"
            "Virtualbox vboxmanage wrapper and infrastructure as code manager\n"
            "\n"
            "usage example\n"
            "\n"
            "   list\n"
            "       # list all projects that have been provisioned\n"
            "       $ boxman list\n"
            "\n"
            "   provision\n"
            "       # provision the configuration in the default config file (conf.yml)\n"
            "       $ boxman provision\n"
            "\n"
            "       # provision using the docker-compose runtime environment\n"
            "       $ boxman --runtime docker-compose provision\n"
            "\n"
            "   snapshot\n"
            "\n"
            "     list\n"
            "       # list snapshots\n"
            "       $ boxman snapshot list\n"
            "\n"
            "     delete\n"
            "       # delete snapshots\n"
            "       $ boxman snapshot delete\n"
            "\n"
            "     take\n"
            "       # snapshot all vms in the default config file\n"
            "       $ boxman snapshot take\n"
            "\n"
            "       # snapshot one or more vms\n"
            "       $ boxman snapshot take --vm=myvm1\n"
            "       $ boxman snapshot take --vm=myvm1,myvm2\n"
            "\n"
            "       # snapshot and set a name for the snapshot (all vms get the same snapshot name)\n"
            "       $ boxman snapshot take --name=mystate1\n"
            "\n"
            "     restore\n"
            "       # restore all vms in the default config file\n"
            "       $ boxman snapshot restore --name=mystate1\n"
            "\n"
            "       # restore one or more vms\n"
            "       $ boxman snapshot restore --vm=myvm1\n"
            "       $ boxman snapshot restore  --vm=myvm1,myvm2\n"
            "\n"
        ),
        formatter_class=RawTextHelpFormatter
    )

    parser.add_argument(
        '--conf',
        type=str,
        help='the name of the configuration file',
        dest='conf',
        default='conf.yml'
    )

    parser.add_argument(
        '--boxman-conf',
        type=str,
        help='the name of the boxman configuration file',
        dest='boxman_conf',
        default='~/.config/boxman/boxman.yml'
    )

    parser.add_argument(
        '--runtime',
        type=str,
        help=(
            'the runtime environment in which to execute provider commands.\n'
            'overrides the "runtime" setting in boxman.yml.\n'
            '  local          - run provider commands directly on the host (default)\n'
            '  docker         - run inside the boxman docker-compose container\n'
        ),
        dest='runtime',
        default=None,
        choices=['local', 'docker']
    )

    parser.add_argument(
        '--version',
        action='count',
        default=0,
        help='display the version and exit'
    )

    subparsers = parser.add_subparsers(help=f"sub-commands for boxman")

    #
    # sub parser for importing images
    #
    parser_import_image = subparsers.add_parser('import-image', help='import an image')
    parser_import_image.set_defaults(func=BoxmanManager.import_image)

    parser_import_image.add_argument(
        '--uri',
        type=str,
        help='the URI of the manifest of the image to import',
        dest='manifest_uri',
        required=True
    )

    parser_import_image.add_argument(
        '--name',
        type=str,
        help='the name to assign to the imported vm',
        dest='vm_name',
        required=False    # the default is used from the manifest
    )

    parser_import_image.add_argument(
        '--directory',
        type=str,
        help='the directory to download/extract the image into',
        dest='vm_dir',
        required=False
    )

    parser_import_image.add_argument(
        '--provider',
        type=str,
        help='the provider to import the image into',
        dest='provider',
        required=False,
        choices=['virtualbox', 'libvirt'])   # figure out how to automate this with the
                                             # supported providers list below

    #
    # sub parser for creating templates from cloud images
    #
    parser_create_templates = subparsers.add_parser(
        'create-templates',
        help='create template VMs from cloud images using cloud-init')
    parser_create_templates.set_defaults(func=BoxmanManager.create_templates)
    parser_create_templates.add_argument(
        '--templates',
        type=str,
        help='comma-separated list of template keys to create (default: all)',
        dest='template_names',
        default=None
    )
    parser_create_templates.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='force creation even if VM already exists',
        dest='force'
    )

    #
    # sub parser for listing the registered projects
    #
    parser_list = subparsers.add_parser('list', help='list all registered projects')
    parser_list.set_defaults(func=BoxmanManager.list_projects)

    #
    # sub parser for provisioning a configuration
    #
    parser_prov = subparsers.add_parser('provision', help='provision a configuration')
    parser_prov.set_defaults(func=BoxmanManager.provision)
    parser_prov.add_argument(
        '--docker-compose',
        action='store_true',
        default=False,
        help='provision using the docker-compose setup',
        dest='docker_compose'
    )
    parser_prov.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='if VMs already exist, deprovision them first and then provision',
        dest='force'
    )
    parser_prov.add_argument(
        '--rebuild-templates',
        action='store_true',
        default=False,
        help='force-rebuild all templates (destroy and recreate) before provisioning',
        dest='rebuild_templates'
    )

    #
    # sub parser for the 'up' subcommand
    #
    parser_up = subparsers.add_parser(
        'up',
        help='bring up the infrastructure: provision if not created, start if powered off')
    parser_up.set_defaults(func=BoxmanManager.up)
    parser_up.add_argument(
        '--docker-compose',
        action='store_true',
        default=False,
        help='use the docker-compose setup',
        dest='docker_compose'
    )
    parser_up.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='if VMs already exist, deprovision them first and then provision',
        dest='force'
    )
    parser_up.add_argument(
        '--rebuild-templates',
        action='store_true',
        default=False,
        help='force-rebuild all templates (destroy and recreate) before provisioning',
        dest='rebuild_templates'
    )

    #
    # sub parser for the 'down' subcommand
    #
    parser_down = subparsers.add_parser(
        'down',
        help='bring down the infrastructure: save or suspend the state of all VMs')
    parser_down.set_defaults(func=BoxmanManager.down)
    parser_down.add_argument(
        '--suspend',
        action='store_true',
        default=False,
        help='suspend (pause) VMs instead of saving their state to disk',
        dest='suspend'
    )

    #
    # sub parser for deprovisioning a configuration
    #
    parser_deprov = subparsers.add_parser('deprovision', help='deprovision a configuration')
    parser_deprov.set_defaults(func=BoxmanManager.deprovision)
    parser_deprov.add_argument(
        '--docker-compose',
        action='store_true',
        default=False,
        help='deprovision using the docker-compose setup',
        dest='docker_compose'
    )

    ##
    ## sub parser for the 'deprovision cluster' subsubcommand
    ##
    #parser_deprov_config = subparsers_deprov.add_parser('config', help='deprovision the whole cluster')
    #parser_deprov_config.set_defaults(func=BoxmanManager.deprovision)

    #
    # sub parser for the 'snapshot' subcommand
    #
    parser_snap = subparsers.add_parser('snapshot', help='manage snapshots the state of the vms')

    subparsers_snap = parser_snap.add_subparsers(
        help=f"sub-commands for boxman snapshot")

    #
    # sub parser for the 'snapshot take' subsubcommand
    #
    parser_snap_take = subparsers_snap.add_parser('take', help='take a snapshot')
    parser_snap_take.set_defaults(func=BoxmanManager.snapshot_take)
    parser_snap_take.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_snap_take.add_argument(
        '--name',
        type=str,
        help='the name of the snapshot',
        dest='snapshot_name',
        default=now.strftime('%Y-%m-%dT%H-%M-%S')
    )
    parser_snap_take.add_argument(
        "--description",
        '-m',
        type=str,
        help='the description of the snapshot',
        dest='snapshot_descr',
        default=''
    )
    parser_snap_take.add_argument(
        '--live',
        action='store_true',
        help='take a snapshot with stopping the vm',
    )
    parser_snap_take.add_argument(
        '--no-live',
        action='store_false',
        help='take a snapshot without stopping the vm',
        dest='live',
    )

    #
    # sub parser for the 'snapshot list' subsubcommand
    #
    parser_snap_list = subparsers_snap.add_parser('list', help='list snapshots')
    parser_snap_list.set_defaults(func=BoxmanManager.snapshot_list)
    parser_snap_list.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'snapshot restore' subsubcommand
    #
    parser_snap_restore = subparsers_snap.add_parser('restore', help='restore the state of vms from snapshot')
    parser_snap_restore.set_defaults(func=BoxmanManager.snapshot_restore)
    parser_snap_restore.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_snap_restore.add_argument(
        '--name',
        type=str,
        help='the name of the snapshot',
        dest='snapshot_name',
        default=None
    )

    #
    # sub parser for the 'snapshot delete' subsubcommand
    #
    parser_snap_delete = subparsers_snap.add_parser('delete', help='delete a snapshot')
    parser_snap_delete.set_defaults(func=BoxmanManager.snapshot_delete)
    parser_snap_delete.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_snap_delete.add_argument(
        '--name',
        type=str,
        help='the name of the snapshot',
        dest='snapshot_name',
        default=None
    )

    #
    # sub parser for the 'control' subcommand
    #
    parser_ctrl = subparsers.add_parser('control', help='control the state of vms')

    subparsers_ctrl = parser_ctrl.add_subparsers(
        help=f"sub-commands for boxman control")

    #
    # sub parser for the 'control suspend' subsubcommand
    #
    parser_ctrl_suspend = subparsers_ctrl.add_parser('suspend', help='suspend vms')
    parser_ctrl_suspend.set_defaults(func=BoxmanManager.suspend_vm)
    parser_ctrl_suspend.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'control resume' subsubcommand
    #
    parser_ctrl_resume = subparsers_ctrl.add_parser('resume', help='resume vms')
    parser_ctrl_resume.set_defaults(func=BoxmanManager.resume_vm)
    parser_ctrl_resume.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'control save' subsubcommand
    #
    parser_ctrl_save = subparsers_ctrl.add_parser('save', help='save the state of vms')
    parser_ctrl_save.set_defaults(func=BoxmanManager.save_vm)
    parser_ctrl_save.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'control start' subsubcommand
    #
    parser_ctrl_start = subparsers_ctrl.add_parser('start', help='start the vms')
    parser_ctrl_start.set_defaults(func=BoxmanManager.start_vm)
    parser_ctrl_start.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_ctrl_start.add_argument(
        '--restore',
        action='store_true',
        default=False,
        help='restore the saved state of the vm before starting',
        dest='restore'
    )

    #
    # sub parser for the 'export' subcommand
    #
    parser_export = subparsers.add_parser('export', help='export the vms')
    parser_export.set_defaults(func=export_config)
    parser_export.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_export.add_argument(
        '--path',
        type=str,
        help='the names of the vms as a csv list',
        dest='path',
        default=None
    )

    #
    # sub parser for the 'import' subcommand
    #
    parser_import_image = subparsers.add_parser('import', help='import the vms')
    parser_import_image.set_defaults(func=import_config)
    parser_import_image.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_import_image.add_argument(
        '--path',
        type=str,
        help='the names of the vms as a csv list',
        dest='path',
    )

    return parser


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
    args = arg_parser.parse_args()

    if args.version:
        print(f'v{boxman.metadata.version}')
        sys.exit(0)

    if not hasattr(args, 'func'):
        arg_parser.print_help()
        sys.exit(1)

    if args.func == BoxmanManager.list_projects:
        manager = BoxmanManager(config=None)
        args.func(manager, None)
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

        # tell the runtime where the project conf.yml lives so bundled
        # assets are deployed next to it (in .boxman/runtime/docker/)
        if manager.runtime_instance.name == 'docker-compose':
            conf_dir = os.path.abspath(os.path.dirname(args.conf))
            manager.runtime_instance.project_dir = conf_dir

            # Extract all workdirs from the project config (one per cluster)
            # and pass them to the runtime so they can be bind-mounted into
            # the container.
            if manager.config and 'clusters' in manager.config:
                workdirs = set()
                for cluster_name, cluster in manager.config['clusters'].items():
                    workdir_raw = cluster.get('workdir')
                    if workdir_raw:
                        workdirs.add(os.path.abspath(
                            os.path.expanduser(workdir_raw)))

                if workdirs:
                    manager.runtime_instance.workdirs = list(workdirs)
                    for wd in workdirs:
                        log.info(f"runtime workdir: {wd}")

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
                # check the uri to get the manifest and find the provider type
                manifest_uri = args.manifest_uri

                # if the image uri is a local file path indicated by file://, load the
                # manifest from there, the manifest is a json file
                if manifest_uri.startswith('file://'):
                    manifest_path = manifest_uri[len('file://'):]
                    with open(manifest_path, 'r') as fobj:
                        manifest = json.load(fobj)
                elif manifest_uri.startswith('http://') or manifest_uri.startswith('https://'):
                    raise NotImplementedError('http/https image uris are not implemented yet')
                else:
                    raise ValueError(f'unsupported image uri: {manifest_uri}')
                provider_type = manifest['provider']

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
            from boxman.docker_compose.docker_compose import DockerCompose

        args.func(manager, args)


if __name__ == '__main__':
    main()
