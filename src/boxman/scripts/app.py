#!/usr/bin/env python
import os
import time
import yaml
from pprint import pprint
import argparse
from argparse import RawTextHelpFormatter
from datetime import datetime

from boxman.virtualbox.vboxmanage import Virtualbox
from boxman.virtualbox.utils import Command
from boxman.utils.io import write_files
from boxman.abstract.hosts_specs import HostsSpecs
from boxman.abstract.providers import Providers
from boxman.abstract.providers import Session


now = datetime.utcnow()
snap_name = now.strftime('%Y-%m-%dT%H:%M:%S')


def parse_args():

    parser = argparse.ArgumentParser(
        description=(
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

    subparsers = parser.add_subparsers(help=f"sub-commands for boxman")

    #
    # sub parser for provisioning a configuration
    #
    parser_prov = subparsers.add_parser('provision', help='provision a configuration')
    parser_prov.set_defaults(func=provision)

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
    vms = parse_vms_list(session, cli_args)
    for vm in vms:
        session.snapshot.take(
            vm,
            snap_name=cli_args.snapshot_name,
            live=cli_args.live)


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
    vms = parse_vms_list(session, cli_args)
    for vm in vms:
        session.snapshot.restore(vm, snap_name=cli_args.snapshot_name)

def provision(session, cli_args):
    conf = session.conf
    project = conf['project']
    cluster_group = list(conf['clusters'].keys())[0]  # one cluster supported for now
    # -------------------- global config ---------------------------

    cluster = conf['clusters'][cluster_group]
    base_image = cluster['base_image']
    cluster_name = cluster_group
    proxy_host = cluster['proxy_host']
    admin_user = cluster['admin_user']
    admin_pass = cluster['admin_pass']
    admin_key_name = cluster['admin_key_name']
    ssh_config = cluster['ssh_config']
    workdir = cluster['workdir']
    # -------------------- end global config -----------------------

    admin_priv_key = os.path.expanduser(os.path.join(workdir, admin_key_name))
    admin_public_key = os.path.expanduser(os.path.join(workdir, admin_key_name + '.pub'))
    ssh_config = os.path.expanduser(os.path.join(workdir, ssh_config))
    workdir = os.path.abspath(os.path.expanduser(workdir))
    if not os.path.isdir(workdir):
        os.makedirs(workdir)

    # write the files specified in the configuration
    if files := cluster.get('files'):
        write_files(files, rootdir=cluster['workdir'])

    # create the guest only NAT networks
    nat_networks = cluster['networks']

    # .. todo:: prefix the vm name (not the hostname) with the cluster group name
    # .. todo:: place each vm in a virtualbox group (like in the ui)
    vms = cluster['vms']
    for vm_name, vm_info in vms.items():
        # set the path of the disk for every disk that is defined
        for disk_info in vm_info['disks']:
            disk_info['disk_path'] = os.path.join(
                workdir,
                f'{cluster_name}_{vm_name}_{disk_info["name"]}.vdi'
            )

    ###############################################################################
    ###############################################################################
    ###############################################################################
    ###############################################################################
    ###############################################################################
    # create the NAT guest only network(s)
    for nat_network, info in nat_networks.items():
        session.natnetwork.add(
            nat_network,
            network=info['network'],
            enable=info['enable'],
            recreate=True
        )

    #
    # clone vm, forward the ssh access port to the vm and boot the vm
    #
    vms = cluster['vms']
    for vm_name, vm_info in vms.items():
        print(f'provision vm {vm_name}')
        pprint(vm_info)

        # clone the vm
        session.removevm(vm_name)
        session.clonevm(vmname=base_image, name=vm_name, basefolder=workdir)
        session.group_vm(vmname=vm_name, groups=os.path.join(f'/{project}', cluster_group))

        # create the meedium and attach the disks
        # get the UUID of the disk from the name of the disk and delete it
        for disk_info in vm_info['disks']:

            disk_path = disk_info['disk_path']
            disk_uuid = session.list('hdds').query_disk_by_path(disk_path)
            if disk_uuid:
                print(f'disk {disk_path} already exists...close and  delete it')
                # .. todo:: implement detaching the disk from the vm before
                #           deleting it but if the vm to which the disk was
                #           attached is off or deleted this is not a problem
                session.closemedium(
                    disk_info['medium_type'], target=disk_uuid, delete=True)

            session.createmedium(
                disk_info['medium_type'],
                filename=disk_path,
                format=disk_info['format'],
                size=disk_info['size'])

            session.storageattach(
                vm_name,
                storagectl=disk_info['attach_to']['controller']['storagectl'],
                port=disk_info['attach_to']['controller']['port'],
                medium=disk_path,
                medium_type=disk_info['attach_to']['controller']['medium_type'])

        # configure the network interfaces
        for interface_no, netowrk_interface_info in enumerate(vm_info['network_adapters']):
            session.modifyvm_network_settings.apply(
                vm_name,
                interface_no + 1,
                netowrk_interface_info
            )

        # create the port forwarding rule
        access_port = vm_info['access_port']
        session.forward_local_port_to_vm(
            vmname=vm_name, host_port=access_port, guest_port="22")
        session.startvm(vm_name)

    # generate the ssh configuration file for easy access without typing much
    # .. todo:: use the ssh config generator in utils.py
    print('write the ssh_config file')
    with open(ssh_config, 'w') as fobj:

        fobj.write('Host *\n')
        fobj.write('    StrictHostKeyChecking no\n')
        fobj.write('    UserKnownHostsFile /dev/null\n')
        fobj.write('\n\n')

        for vm_name, vm_info in vms.items():
            fobj.write(f'Host {vm_info["hostname"]}\n')
            fobj.write(f'    Hostname {proxy_host}\n')
            fobj.write(f'    User {admin_user}\n')
            fobj.write(f'    Port {vm_info["access_port"]}\n')
            fobj.write(f'    IdentityFile {admin_priv_key}\n')
            fobj.write('\n\n')

    # generate the ssh priv/pub key pair if it does not exist
    if not os.path.exists(admin_priv_key):
        cmd = f'ssh-keygen -t ed25519 -a 100 -f {admin_priv_key} -q -N ""'
        Command(cmd).run()
        for fpath in [admin_priv_key, admin_public_key]:
            _fpath = os.path.abspath(os.path.expanduser(fpath))
            assert os.path.isfile(_fpath)
        print('admin priv/pub key generated successfully')

    # wait for all the vms to be ssh'able
    print("wait for vms to be ssh'able")
    for vm_name, vm_info in vms.items():
        print(f'vm: {vm_name}')
        ssh_status = session.wait_for_ssh_server_up(
            host='localhost',
            port=vm_info['access_port'],
            timeout=60,
            n_try=20
        )
        if ssh_status is True:
            print(f"vm {vm_name} is ssh'able")

        # add the ssh-key of the admin account to enable passwordless login
        # .. todo:: make sure that the machine is sshable by executing a ssh
        #           command that echo's the hostname or something and repeat
        #           until it succeeds with max n tries...etc...
        n_try = 5
        t_retry = 10
        ssh_success = False
        print(f'try to add ssh key to {vm_name}')
        for try_no in range(n_try):
            print(f'trial {try_no}')

            process = Command(
                f'sshpass -p {admin_pass} ssh-copy-id -p {vm_info["access_port"]} '
                f'-i {admin_public_key} -o StrictHostKeyChecking=no '
                f'-o UserKnownHostsFile="/dev/null" {admin_user}@localhost'
            ).run(capture=True)

            print(process.stdout)
            print(process.stderr)

            if process.process.returncode == 0:

                cmd = Command(f'ssh -F {ssh_config} {vm_info["hostname"]} hostname')
                process = cmd.run(capture=True)
                print('-' * 10)
                print(f'stdout: {process.stdout}')
                print(f'stderr: {process.stderr}')
                print('-' * 10)
                # .. todo:: replace the osboxes and hostname cmd check with
                # something more reliable
                if len(process.stdout.strip()) > 0:
                    ssh_success = True

            if ssh_success:
                print('sucessfully sshed to vm')
                break

            time.sleep(t_retry)

        if ssh_success is False:
            raise ValueError('could not add ssh key')

    print('to ssh to a certain host e.g mgmt01:')
    print(f'>>> ssh -F {ssh_config} mgmt01')

    print('to run ansible:')
    print(
        f'>>> ansible --ssh-common-args="-F {ssh_config}" -i /path/to/inventory all -m ping')


def main():

    arg_parser = parse_args()
    args = arg_parser.parse_args()

    with open(args.conf) as fobj:
        conf = yaml.safe_load(fobj.read())

    # .. todo:: implement guessing the provider from the config file
    # .. todo:: is it worth to think/design for multiple providers
    # .         in the same config?
    # session = Session(my_provider)
    session = Virtualbox(conf)

    args.func(session, args)


if __name__ == '__main__':
    main()



