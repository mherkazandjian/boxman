"""Push qcow2 images (with optional metadata) to an OCI registry via oras."""

from __future__ import annotations

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

    files_to_push = [str(qcow2_file)]

    if metadata_path is not None:
        metadata_file = Path(metadata_path)
        if not metadata_file.is_file():
            raise RuntimeError(f"metadata file not found: {metadata_path}")
        files_to_push.append(str(metadata_file))

    cmd = ["oras", "push", image_ref] + files_to_push

    try:
        result = subprocess.run(
            cmd,
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
