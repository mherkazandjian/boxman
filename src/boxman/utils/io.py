import errno
import os
import secrets
import tempfile

from boxman import log


def mkstemp_in(directory: str, prefix: str = '',
               suffix: str = '') -> tuple[int, str]:
    """
    ``tempfile.mkstemp(dir=directory)``, but the file is created in
    *directory* as given.

    Since Python 3.12 mkstemp makes *directory* absolute before it creates
    the file, and that folds ``link/..`` as text into what can be another
    directory than the one the kernel resolves it to (#260). A file staged
    there is not the one written by its name in *directory*, nor beside the
    target it is renamed over.

    :param directory: where to create the file
    :param prefix: the start of its name
    :param suffix: the end of its name
    :return: a descriptor open on it for reading and writing, and its
        absolute name: *directory* joined onto the working directory when
        relative, with nothing folded
    :raises OSError: if it cannot be created
    """
    if not os.path.isabs(directory):
        directory = os.path.join(os.getcwd(), directory)
    # what mkstemp does: a new file of our own, never one through a symlink
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(tempfile.TMP_MAX):
        path = os.path.join(directory,
                            f'{prefix}{secrets.token_hex(4)}{suffix}')
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, 'no unused temporary name', directory)


def write_files(files: dict[str, str], rootdir: str | None = None) -> None:
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

        # Do not overwrite existing env.sh or ansible.cfg
        basename = os.path.basename(fpath)
        if basename in ('env.sh', 'ansible.cfg') and os.path.exists(fpath):
            log.warning(f'{basename} already exists, skipping: {fpath}')
            continue

        log.info(f'provision file {fpath}')

        dirpath = os.path.dirname(fpath)
        if not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(fpath, 'w') as fobj:
            fobj.write(files[_fpath])
