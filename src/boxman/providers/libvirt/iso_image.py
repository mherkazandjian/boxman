"""
Build a small ISO 9660 image from a few files: a cloud-init seed, an answer ISO.

The cloud-init seed of a template (label ``cidata``) and the answer ISO of an
unattended Windows install (label ``BOXMAN_ANSWER``) are the same kind of
thing: a handful of files at the root of a CD-ROM the guest reads at its
first boot. genisoimage, mkisofs and xorrisofs take the same flags for that,
and distros ship different ones (Debian's xorriso package has only
xorrisofs), so each is tried in turn.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable, Iterable

from boxman import log
from boxman.loggers.logger import is_verbose

#: the tools tried, in order; they take the same flags
ISO_TOOLS = ("genisoimage", "mkisofs", "xorrisofs")


def build_iso(run: Callable[..., object], iso_path: str, label: str,
              files: Iterable[str]) -> str | None:
    """
    Write *files* to the root of a new ISO image at *iso_path*.

    Joliet and Rock Ridge both, so Windows and Linux guests read the long
    names. Each file lands under its basename.

    Args:
        run: runs one shell command and returns its result (``.ok``): the
            caller decides where it runs and with which privileges.
        iso_path: the image to write; whatever is there is overwritten.
        label: the volume ID (``cidata`` for a NoCloud seed).
        files: the paths to put on it.

    Returns:
        The tool that built it, or None when none of them could.
    """
    paths = " ".join(shlex.quote(path) for path in files)
    for tool in ISO_TOOLS:
        result = run(
            f"{tool} -output {shlex.quote(iso_path)} "
            f"-volid {shlex.quote(label)} -joliet -rock {paths}",
            hide=not is_verbose(logging.DEBUG), warn=True)
        if result.ok:
            return tool
    log.error(f"could not build the ISO {iso_path}: install one of "
              f"{', '.join(ISO_TOOLS)}")
    return None
