from .utils import Command


class NetworkSettings:

    def __init__(self, init=True):
        pass

    def apply(self, vm, nic_num, options):

        cmd = f'vboxmanage modifyvm {vm} --nic{nic_num} '

        # figure out the network option cmd line args
        network_type = None
        network_name = None
        if 'attached_to' in options:
            network_type = list(options['attached_to'].keys()).pop()
            network_name = options['attached_to'][network_type]

            if network_type == 'natnetwork':
                cmd += 'natnetwork '
                cmd += f'--nat-network{nic_num} {network_name} '
            elif network_type == 'nat':
                cmd += 'nat '

        if 'cableconnected' in options:
            cmd += f'--cableconnected{nic_num} {options["cableconnected"]} '

        Command(cmd).run()

