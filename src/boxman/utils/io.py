import os


def write_files(files, rootdir=None):
    """

    :param files:
    :return:
    """
    for _fpath in files:
        if rootdir:
            fpath = os.path.join(rootdir, _fpath)
        else:
            fpath = _fpath

        fpath = os.path.expanduser(fpath)
        print(f'provision file {fpath}')

        dirpath = os.path.dirname(fpath)
        if not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(fpath, 'w') as fobj:
            fobj.write(files[_fpath])

