import re
from .utils import Command


class ShowVmInfo:
    def __init__(self):
        pass
    def vminfo(self, vm):
        """
        return the detailed info a vm
        """
        cmd = ""
        cmd += f"vboxmanage showvminfo --details --machinereadable {vm}"
        process = Command(cmd)
        process.run(capture=True)

        return dict(re.findall(r"(.*)\=\"?(.*)\"?", process.stdout))



