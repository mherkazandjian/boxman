from .vbox_showvminfo import ShowVmInfo


class VM:
    def __init__(self, name=None, uuid=None):
        self.name = name
        self.uuid = uuid
        self._info = None

    @property
    def info(self):
        if self._info is None:
            self._info = ShowVmInfo().vminfo(self.name)
        return self._info

