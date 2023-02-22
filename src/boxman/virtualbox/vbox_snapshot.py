from .utils import Command
import datetime
from datetime import datetime


class Snapshot:
    """
    Provide functionality for creating and using and manipulating vm snapshots
    """
    def __init__(self, session=None, init=True):
        """
        Constructor
        """
        self.session = session

    def take(self, vm_name: str, snap_name: str = None, description: str = '', live: bool = True):
        """
        Take a snapshot of a vm

        :param vm_name: Then name or uuid of the vm
        :param snap_name: The name of the snapshot
        :param description: The descritpion of the snapshot
        :param live: require the snapshot to be taken while the vm is still running
        """

        # .. tood:: add check that makes sure that the snapshot has been taken
        if not snap_name:
            now = datetime.utcnow()
            snap_name = now.strftime('%Y-%m-%dT%H:%M:%S')

        # compose the command and set the arguments
        cmd = f'vboxmanage snapshot {vm_name} take {snap_name} '
        if description:
            cmd += f'--description {description}'
        if live:
            cmd += '--live '

        Command(cmd).run()

    def list(self, vm_name: str):
        """
        list a snapshot

        :param vm_name: The name of the vm
        """
        assert vm_name, 'the name of the snapshot must be provided'

        # compose the command and set the arguments
        cmd = f'vboxmanage snapshot {vm_name} list'

        Command(cmd).run()


    def delete(self, vm_name: str, snap_name: str = None):
        """
        Delete a snapshot

        :param vm_name: The name of the vm
        :param snap_name: The name of the snapshot to be deleted
        """
        assert vm_name, 'the name of the snapshot must be provided'

        # compose the command and set the arguments
        cmd = f'vboxmanage snapshot {vm_name} delete {snap_name}'

        Command(cmd).run()

    def restore(self, vm_name, snap_name=None):
        """
        Restore the state of a vm from a snapshot

        :param vm_name: The name of the vm
        :param snap_name: The name of the snapshot to be deleted
        """
        assert vm_name, 'the name of the snapshot must be provided'

        self.session.savestate(name=vm_name)
        # compose the command and set the arguments
        cmd = f'vboxmanage snapshot {vm_name} restore {snap_name} '

        Command(cmd).run()

        self.session.startvm(vm_name)


class SnapshotCluster:
    # .. todo:: not implemented yet
    def __init__(self):
        pass
