import re
from .utils import Command


class NatNetwork:

    def __init__(self, init=True):
        self._list = None

        if init:
            self._list = NatNetworkList().list()

    @property
    def list(self):
        self._list = NatNetworkList().list()
        return self._list

    def stop(self, network_name):
        if network_name in self.list:
            cmd = f'vboxmanage natnetwork stop --netname {network_name}'
            Command(cmd).run()
        else:
            print(f'WARN: nat network {network_name} is not defined')

    def remove(self, network_name):
        if network_name in self.list:
            cmd = f'vboxmanage natnetwork remove --netname {network_name}'
            Command(cmd).run()
        else:
            print(f'WARN: nat network {network_name} is not defined')

    def add(self, network_name, network=None, enable=False, recreate=False, dhcp='on'):
        if network_name in self.list and recreate:
            self.stop(network_name)
            self.remove(network_name)

        cmd = ''
        cmd += f'vboxmanage natnetwork add --netname {network_name} '
        assert network is not None
        cmd += f'--network "{network}" '
        if enable:
            cmd += '--enable '

        if dhcp in [None, False]:
            dhcp = 'off'
        assert dhcp in ['on', 'off']
        cmd += f'--dhcp {dhcp} '

        Command(cmd).run()


class NatNetworkList:
    """
    """
    def __init__(self):
        """
        """
        pass

    def list(self):
        """
        Return a list of the NAT network

        The output of the command:

            vboxmanage natnetwork list

        if parsed and a dictionary of the defined networks is returned

        sample output of the command:

            # sample 1
                NAT Networks:

                Name:        NatNetwork
                Network:     10.0.1.0/24
                Gateway:     10.0.1.1
                IPv6:        No
                Enabled:     Yes


                Name:        NatNetwork1-cluster
                Network:     10.1.1.0/24
                Gateway:     10.1.1.1
                IPv6:        No
                Enabled:     Yes

                2 networks found

                # sample 2
                NAT Networks:

                0 networks found
        """
        process = Command('vboxmanage natnetwork list')
        process.run(capture=True)

        networks = {}
        patterns = ['Name', 'Network', 'Gateway', 'IPv6', 'Enabled']
        for block in re.finditer(r"(Name.*(?:\n.+)+)", process.stdout):
            info = {}
            for pattern in patterns:
                info[pattern.lower()] = re.findall(
                    rf"(?i)^{pattern}\:(.*$)",
                    block.group(0),
                    re.MULTILINE
                )[0].strip()
            networks[info['name']] = info

        return networks

