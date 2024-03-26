import re
from .utils import Command


def run_list_sub_command(sub_command):
        sub_commands = {
            'vms': ListVms,
            'hdds': ListHdds
        }
        return sub_commands[sub_command]()


class ListVms:
    def __init__(self, run=True):
        self.name2uuid = None
        self.uuid2name = None
        if run:
            self.run()

    def run(self):
        """
        return and set the dict that map vm names to uuids and vice-versa
        """
        process = Command('vboxmanage list vms')
        process.run(capture=True)

        regex = r"(?:\"(.*)\") (?:\{(.*)\})"
        self.name2uuid = dict(re.findall(regex, process.stdout))
        self.uuid2name = {uuid:name for name, uuid in self.name2uuid.items()}


class ListHdds:
    def __init__(self, run=True):
        self.hdds = None
        if run:
            self.hdds = self.run()

    def run(self):
        """
        return and set the dict of the hdd uuids and their info
        """
        process = Command('vboxmanage list hdds')
        process.run(capture=True)

        hdds = {}
        patterns = ["UUID", "Parent UUID", "State", "Type", "Location", "Storage format", "Capacity", "Encryption"]
        for block in re.finditer(r"(UUID.*(?:\n.+)+)", process.stdout):
            info = {}
            for pattern in patterns:
                info[pattern] = re.findall(
                    rf"(?i)^{pattern}\:(.*$)",
                    block.group(0),
                    re.MULTILINE
                )[0].strip()
            hdds[info['UUID']] = info

        return hdds

    def query_disk_by_path(self, disk_path):
        for disk_uuid, disk_info in self.hdds.items():
            print(f'{disk_uuid}: {disk_info["Location"]}')
            if disk_info['Location'] == disk_path:
                return disk_uuid
        else:
            return None
