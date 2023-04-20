import time
import re
from telnetlib import Telnet
from .utils import Command
from .utils import log

from . import vbox_natnetwork
from . import vbox_showvminfo
from . import vbox_modifyvm
from . import vbox_list
from . import vbox_snapshot


class Virtualbox:

    def __init__(self, conf):
        """
        Constructor
        """
        self.natnetwork = vbox_natnetwork.NatNetwork()
        self.showvminfo = vbox_showvminfo.ShowVmInfo()
        self.list = vbox_list.run_list_sub_command
        self.modifyvm_network_settings = vbox_modifyvm.NetworkSettings()
        self.snapshot = vbox_snapshot.Snapshot(session=self)
        self.conf = conf

    def clonevm(self,
                vmname: str = None,      # myvm
                snapshot: str = None,    # <uuid>|<name>
                mode: str = 'all',       # machine|machineandchildren|all
                name: str = None,        # new name of the vm
                basefolder: str = None,  # /path/to/vm/data
                uuid: str = None,        # see uuidgen, e.g eef2ffe9-6777-46b3-b77d-95ffdbaa652a
                register: bool = True):   #
        """
        create a new vm by clonining

        .. note:: this is a syncroneous process
        """

        assert vmname is not None
        assert name is not None

        cmd = ""
        cmd += "vboxmanage clonevm "
        cmd += f"{vmname} "
        cmd += "" if snapshot is None else f"--snapshot {snapshot} "
        cmd += f"--mode {mode} "
        cmd += f"--name {name} "
        cmd += "" if basefolder is None else f"--basefolder {basefolder} "
        cmd += "" if uuid is None else f"--uuid {uuid} "
        cmd += "" if register is None else "--register "

        process = Command(cmd)
        process.run()
        return process

    def forward_local_port_to_vm(self,
                                 vmname: str = None,
                                 host_port: str = None,
                                 guest_port: str = None):
        """
        Forward the ssh port to the guest vms

        :param metadata: the full metadata dict
        """
        cmd = (
            f'vboxmanage modifyvm "{vmname}" '
            f'--natpf1 "guestssh,tcp,,{host_port},,{guest_port}"'
        )
        Command(cmd).run()

    def startvm(self, name: str = None):
        """
        Start the virtual machine
        """
        Command(f'vboxmanage startvm {name} --type headless').run()

    def poweroff(self, name: str = None):
        """
        Power-off a vm - acpi
        """
        Command(f"vboxmanage controlvm {name} poweroff").run()

    def savestate(self, name: str = None):
        """
        Save the state of the vm

        :param name: the name or uuid of the vm
        """
        Command(f"vboxmanage controlvm {name} savestate").run()

    def suspend(self, name: str = None):
        """
        Suspend the vm

        :param name: the name or uuid of the vm
        """
        Command(f"vboxmanage controlvm {name} pause").run()

    def resume(self, name: str = None):
        """
        Resume the vm

        :param name: the name or uuid of the vm
        """
        Command(f"vboxmanage controlvm {name} resume").run()


    def unregistervm(self, name: str = None):
        """
        Remove/delete a virtualmachine

        - check that the vm exists in "vboxmanage list vms"
            "ubuntu-20.04-vanilla" {8234b7cf-fc60-48ea-96e7-67aed359cca8}
            "ubuntu-20.04-vagrant" {32857215-619d-4b7b-9685-29edcc354e5a}
            "centos8-minimal-base" {64e766ca-6630-4bfb-9aa9-c6b4c4c6c899}
        - if vm exists un-register it and remove it
        """
        command = Command("vboxmanage list vms")
        command.run(capture=True)
        found = False
        for line in filter(lambda x: x.strip() != '', command.stdout.splitlines()):
            _name, _ = line.split(' {')
            if _name.replace('"', '').strip() == name:
                found = True
                print(f"vm {name} found, proceed to unregister/delete it")
                break

        if found:
            print(f"delete vm {name}...")
            Command(f"vboxmanage unregistervm {name} --delete").run()

    def vm_is_running(self, name: str = None) -> bool:
        """
        Retrun True if the vm is vm_is_running

        The command output looks like:
            "foo-bar-vm" {79992a4d-7f9f-4557-9054-f5c9ac44538a}
        """
        command = Command("vboxmanage list runningvms")
        command.run(capture=True)
        found = False
        for line in filter(lambda x: x.strip() != '', command.stdout.splitlines()):
            _name, _ = line.split(' {')
            if _name.replace('"', '').strip() == name:
                found = True
                break

        return found

    def removevm(self, name: str = None):
        """
        Remove the virtual machine if it is running safely
        """
        if self.vm_is_running(name):
            self.poweroff(name)

        # .. todo:: check that the vm exists
        self.unregistervm(name)

    def wait_for_ssh_server_up(self,
                               host: str = None,
                               port: str = None,
                               timeout: int = 10,
                               n_try: int = 5,
                               no_raise: bool = True):
        """
        Return True if the ssh server is up otherwise False

        uses telnet, success criterion SSH string is read by telnet
        """
        is_up = False
        match_bytes = b'SSH'
        for attempt_no in range(n_try):

            print(f'attempt {attempt_no} to check ssh server status')
            try:
                with Telnet(host, int(port)) as tn:
                    data = tn.read_until(match_bytes, timeout=timeout)
                    if data == match_bytes:
                        is_up = True
                        break
            except EOFError:
                print('telnet EOFError, try again')

            if is_up:
                break
            else:
                time.sleep(5)

        if not is_up and no_raise is False:
            raise ValueError("ssh server is not up")

        return is_up

    def closemedium(self, medium_type, target=None, delete=False):
        """
        Close a medium and optionally delete it

        :param medium_type: disk | dvd | floppy
        :param target: uuid | filename
        """
        cmd = ""
        cmd += "vboxmanage closemedium "
        cmd += f'{medium_type} '
        assert medium_type in ['disk', 'dvd', 'floppy']
        cmd += f'{target} '
        if delete:
            cmd += '--delete'

        Command(cmd).run()

    def createmedium(self,
                     medium_type,
                     filename=None,
                     format='VDI',
                     size=None,
                     variant='Standard'):
        """
        Create a medium

        :param medium_type: disk | dvd | floppy
        :param filename: The name / path of the file
        :param format: vdi | vmdk | vhd
        :param size: size in MB
        """
        cmd = ""
        cmd += "vboxmanage createmedium "
        assert medium_type in ['disk', 'dvd', 'floppy']
        cmd += f'{medium_type} '
        cmd += f'--filename {filename} '
        cmd += f'--format {format} '
        assert size > 0
        cmd += f'--size {size} '
        cmd += f'--variant {variant} '
        Command(cmd).run()

    def storageattach(self,
                      vm,
                      storagectl=None,
                      port=None,
                      medium=None,
                      medium_type=None):
        """
        Attach a storage medium to a storage controller to a vm

        :param vm: the name or the uuid of a vm
        :param storagectl: the name of the storage controller
        :param port: the port # on the controller where the medium will be attached
        :param medium: the path of the medium (file/name|path)
        :param medium_type: the type of the medium e.g hdd
        """
        cmd = ""
        cmd += "vboxmanage storageattach "
        cmd += f'{vm} '
        assert storagectl is not None
        cmd += f'--storagectl {storagectl} '
        assert port is not None
        cmd += f'--port {port} '
        assert medium is not None
        cmd += f'--medium {medium} '
        assert medium_type is not None
        cmd += f'--type {medium_type} '

        Command(cmd).run()

    def group_vm(self, vmname: str = None, groups: str = None):
        """
        Set the group name of the vm

        :param vmname: The name or uuid of the vm
        :param groups: The groups of the vm, e.g /foo or /foo1,/foo2 or /foo/bar
        """
        Command(f'vboxmanage modifyvm {vmname} --groups {groups}').run()

    def vminfo(self, vm):
        """
        return the detailed info a vm
        """
        cmd = ""
        cmd += f"vboxmanage showvminfo --details --machinereadable {vm}"
        process = Command(cmd)
        process.run(capture=True)

        return dict(re.findall(r"(.*)\=\"?(.*)\"?", process.stdout))

    def export_vm(self, vmname: str = None, path: str = None):
        """
        Export a vm to an ovf file

        :param vmname: The name or uuid of the vm
        :param output: The output file
        """
        cmd = ""
        cmd += f'vboxmanage export {vmname} --output {path}'
        process = Command(cmd)
        process.run(capture=True)