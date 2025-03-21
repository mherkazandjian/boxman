#!/usr/bin/env python
import os
import sys
import time
import yaml
from pprint import pprint
import argparse
from argparse import RawTextHelpFormatter
from datetime import datetime
import shutil
from multiprocess import Process

import boxman
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.session import LibVirtSession
from boxman.virtualbox.vboxmanage import Virtualbox
from boxman.virtualbox.utils import Command
from boxman.utils.io import write_files
from boxman.abstract.hosts_specs import HostsSpecs
#from boxman.abstract.providers import Providers
from boxman.abstract.providers import ProviderSession as Session


now = datetime.utcnow()
snap_name = now.strftime('%Y-%m-%dT%H:%M:%S')


def parse_args():

    parser = argparse.ArgumentParser(
        description=(
            f"Boxman version {boxman.metadata.version}\n"
            "Virtualbox vboxmanage wrapper and infrastructure as code manager\n"
            "\n"
            "usage example\n"
            "\n"
            "   provision"
            "       # provision the configuration in the default config file (conf.yml)\n"
            "       $ boxman provision\n"
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
        '--version',
        action='count',
        default=0,
        help='display the version and exit'
    )

    subparsers = parser.add_subparsers(help=f"sub-commands for boxman")

    #
    # sub parser for provisioning a configuration
    #
    parser_prov = subparsers.add_parser('provision', help='provision a configuration')
    parser_prov.set_defaults(func=BoxmanManager.provision)

    #
    # sub parser for deprovisioning a configuration
    #
    parser_deprov = subparsers.add_parser('deprovision', help='deprovision a configuration')
    parser_deprov.set_defaults(func=BoxmanManager.deprovision)

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
    parser_snap_take.set_defaults(func=snapshot_take)
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
        default=now.strftime('%Y-%m-%dT%H:%M:%S')
    )
    parser_snap_take.add_argument(
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
    parser_snap_list.set_defaults(func=snapshot_list)
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
    parser_snap_restore.set_defaults(func=snapshot_restore)
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
    parser_snap_delete.set_defaults(func=snapshot_delete)
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
    parser_ctrl_suspend.set_defaults(func=machine_suspend)
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
    parser_ctrl_resume.set_defaults(func=machine_resume)
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
    parser_ctrl_save.set_defaults(func=machine_save)
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
    parser_ctrl_start.set_defaults(func=machine_start)
    parser_ctrl_start.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
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
    parser_import = subparsers.add_parser('import', help='import the vms')
    parser_import.set_defaults(func=import_config)
    parser_import.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_import.add_argument(
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


def main():

    arg_parser = parse_args()
    args = arg_parser.parse_args()

    if args.version:
        print(f'v{boxman.metadata.version}')
        sys.exit(0)

    manager = BoxmanManager(config=args.conf)

    # Get the provider and its type
    provider = manager.config.get('provider', {'virtualbox': {}})
    provider_type = list(manager.config['provider'].keys())[0]

    if provider_type == 'virtualbox':
        session = Virtualbox(manager.config)
    elif provider_type == 'libvirt':
        session = LibVirtSession(manager.config)
        manager.provider = session
    elif provider == 'docker-compose':
        raise NotImplementedError('docker-compose is not implemented yet')
        from boxman.docker_compose.docker_compose import DockerCompose

    args.func(manager, args)


if __name__ == '__main__':
    main()
