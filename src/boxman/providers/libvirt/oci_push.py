"""Push qcow2 images (with optional metadata) to an OCI registry via oras."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def push_oci_image(
    image_ref: str,
    qcow2_path: str,
    metadata_path: Optional[str] = None,
) -> None:
    """Push a local qcow2 image and optional metadata to an OCI registry.

    Authentication is delegated to oras and follows oras-supported methods:

    - Environment variables: ``ORAS_USERNAME``, ``ORAS_PASSWORD``
    - Config file: ``~/.oras/config.json``
    - Interactive prompt if credentials are needed

    Args:
        image_ref: OCI image reference (e.g. ``"registry.com/repo:tag"``)
        qcow2_path: Path to the qcow2 file to push.
        metadata_path: Optional path to a ``vmimage.json`` metadata file.

    Raises:
        ValueError: If *image_ref* is empty.
        RuntimeError: If file validation fails or the oras push command fails.
        FileNotFoundError: If the oras CLI is not on PATH.
    """
    if not image_ref or not str(image_ref).strip():
        raise ValueError("image_ref must be a non-empty string")

    _oras_push(image_ref=image_ref, qcow2_path=qcow2_path, metadata_path=metadata_path)


def _oras_push(
    image_ref: str,
    qcow2_path: str,
    metadata_path: Optional[str] = None,
) -> None:
    """Push an OCI artifact to a registry using the oras CLI.

    Raises:
        RuntimeError: If file validation fails or oras push command fails.
        FileNotFoundError: Wrapped as RuntimeError when oras is not on PATH.
    """
    qcow2_file = Path(qcow2_path)
    if not qcow2_file.is_file():
        raise RuntimeError(f"qcow2 file not found: {qcow2_path}")

    # oras rejects absolute file paths: the path becomes the artifact's layer
    # title (org.opencontainers.image.title) and an absolute title would enable
    # path traversal on pull. Push from the file's directory using basenames so
    # the titles are clean relative names (e.g. 'disk.qcow2') that the pull side
    # recreates safely.
    #
    # Use absolute-but-unresolved paths (os.path.abspath does not follow
    # symlinks): cwd + basename then name *exactly* the file the user passed.
    # Pairing a symlink-resolved directory with the symlink's own basename would
    # point oras at a non-existent file, and "same directory" should mean the
    # directory the files were placed in, not their symlink targets.
    qcow2_abs = Path(os.path.abspath(qcow2_path))
    work_dir = str(qcow2_abs.parent)
    files_to_push = [qcow2_abs.name]

    if metadata_path is not None:
        metadata_arg = Path(metadata_path)
        # A bare relative metadata path is interpreted as co-located with the
        # qcow2 (honouring the co-location contract below), not resolved against
        # the process CWD — otherwise `--metadata vmimage.json` would only work
        # when invoked from the qcow2's own directory.
        if metadata_arg.is_absolute():
            metadata_abs = Path(os.path.abspath(metadata_path))
        else:
            metadata_abs = qcow2_abs.parent / metadata_arg.name
        if not metadata_abs.is_file():
            raise RuntimeError(f"metadata file not found: {metadata_path}")
        if str(metadata_abs.parent) != work_dir:
            raise RuntimeError(
                "qcow2 and metadata must be in the same directory to push "
                f"(qcow2 dir: {work_dir}, metadata: {metadata_abs})")
        files_to_push.append(metadata_abs.name)

    cmd = ["oras", "push", image_ref] + files_to_push

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "oras CLI not found. Please install 'oras' and ensure it is on PATH."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            "oras push failed for '{ref}'.\n"
            "command: {cmd}\n"
            "exit code: {code}\n"
            "stdout:\n{stdout}\n"
            "stderr:\n{stderr}\n".format(
                ref=image_ref,
                cmd=" ".join(cmd),
                code=result.returncode,
                stdout=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
            )
        )
