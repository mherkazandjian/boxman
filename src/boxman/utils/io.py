import os
from typing import Dict, Optional

from boxman import log

def write_files(files: Dict[str, str], rootdir: Optional[str] = None) -> None:
    """
    Write files to the filesystem. The files are specified as a dictionary
    where the keys are the file paths and the values are the file contents.

    :param files: Dictionary mapping file paths to their contents
    :param rootdir: Optional root directory where files will be created
    :return: None
    """
    for _fpath in files:
        if rootdir:
            fpath = os.path.join(rootdir, _fpath)
        else:
            fpath = _fpath

        fpath = os.path.normpath(os.path.expanduser(fpath))
        log.info(f'provision file {fpath}')

        dirpath = os.path.dirname(fpath)
        if not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(fpath, 'w') as fobj:
            fobj.write(files[_fpath])
