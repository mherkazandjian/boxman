#!/usr/bin/env python3
"""
Repackage a Poetry-built wheel so that `containers/` and `boxes/` are
installed into <prefix>/share/boxman/ via the wheel .data directory.

Poetry includes these files at the wheel root (next to the package), but
pip ignores top-level extras that aren't Python packages.  The wheel spec
defines a ``{name}-{ver}.data/data/`` directory whose contents are installed
relative to ``sys.prefix``, which is exactly what we need.

Usage:
    python scripts/repackage_wheel.py dist/boxman-*.whl
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sys
import zipfile
from base64 import urlsafe_b64encode
from pathlib import Path

# Directories to relocate and their target under share/boxman/
RELOCATIONS = {
    "containers/": "share/boxman/containers/",
    "boxes/": "share/boxman/boxes/",
}


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _data_dir(wheel_path: str) -> str:
    """Derive the .data directory name from the wheel filename."""
    basename = os.path.basename(wheel_path)
    m = re.match(r"^(.+?-.+?)-", basename)
    if not m:
        raise ValueError(f"Cannot parse wheel filename: {basename}")
    return m.group(1) + ".data/data"


def repackage(wheel_path: str) -> None:
    data_prefix = _data_dir(wheel_path)
    tmp_path = wheel_path + ".tmp"

    with zipfile.ZipFile(wheel_path, "r") as src, \
         zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:

        record_name = None
        records: list[tuple[str, str, str]] = []

        for info in src.infolist():
            name = info.filename

            # Find the RECORD file path
            if name.endswith("/RECORD"):
                record_name = name
                continue  # rewrite at the end

            # Check if this file should be relocated
            new_name = None
            for src_prefix, dst_prefix in RELOCATIONS.items():
                if name.startswith(src_prefix):
                    new_name = f"{data_prefix}/{dst_prefix}{name[len(src_prefix):]}"
                    break

            data = src.read(info.filename)
            target_name = new_name if new_name else name

            # Preserve the ZipInfo metadata (timestamps, permissions)
            new_info = info
            new_info.filename = target_name
            dst.writestr(new_info, data)

            records.append((target_name, _record_hash(data), str(len(data))))

        # Rewrite RECORD
        if record_name is None:
            raise ValueError("No RECORD file found in wheel")

        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in records:
            writer.writerow(row)
        writer.writerow((record_name, "", ""))  # RECORD itself is unverified
        record_data = buf.getvalue().encode("utf-8")
        dst.writestr(record_name, record_data)

    os.replace(tmp_path, wheel_path)
    print(f"Repackaged: {wheel_path}")
    # Show relocated files
    for src_prefix, dst_prefix in RELOCATIONS.items():
        print(f"  {src_prefix}* -> <prefix>/{dst_prefix}*")


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <wheel-file> [<wheel-file> ...]", file=sys.stderr)
        sys.exit(1)

    for whl in sys.argv[1:]:
        repackage(whl)


if __name__ == "__main__":
    main()
