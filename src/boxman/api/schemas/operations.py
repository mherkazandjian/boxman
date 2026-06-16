"""Request bodies for snapshot/storage/control/run/template/image/netlab ops."""

from __future__ import annotations

from pydantic import BaseModel, Field

# A box selector mirrors the CLI ``--vms`` flag ("all" or a list of names).
Boxes = str | list[str]


# ── snapshots ─────────────────────────────────────────────────────────


class SnapshotTakeRequest(BaseModel):
    boxes: Boxes = "all"
    name: str | None = Field(None, description="snapshot name (default: timestamp)")
    description: str | None = None
    live: bool | None = Field(None, description="live snapshot (None → CLI default)")
    compress_memory: bool = False
    memory_compress_level: int | None = None


class SnapshotScopeRequest(BaseModel):
    """For restore/delete where the snapshot name comes from the path."""

    boxes: Boxes = "all"


class SnapshotCollapseRequest(BaseModel):
    boxes: Boxes = "all"
    to: str = Field(..., description="oldest snapshot to keep revertable")
    no_shutdown: bool = False
    dry_run: bool = False


# ── storage ───────────────────────────────────────────────────────────


class StorageTrimRequest(BaseModel):
    boxes: Boxes = "all"
    dry_run: bool = False


class StorageCompactRequest(BaseModel):
    boxes: Boxes = "all"
    method: str = "auto"
    no_shutdown: bool = False
    drop_snapshots: bool = False
    dry_run: bool = False


class StorageOptimizeRequest(BaseModel):
    boxes: Boxes = "all"
    method: str = "auto"
    skip_trim: bool = False
    skip_compact: bool = False
    no_shutdown: bool = False
    drop_snapshots: bool = False
    dry_run: bool = False


class StorageCompressRequest(BaseModel):
    boxes: Boxes = "all"
    level: int | None = None
    decompress: bool = False


# ── control ───────────────────────────────────────────────────────────


class ControlRequest(BaseModel):
    boxes: Boxes = "all"
    # only meaningful for 'start'; ignored by the other control ops
    restore: bool = False


class PxeBootRequest(BaseModel):
    vm: str = Field(..., description="full domain name of the VM to PXE boot")
    expected_ip: str | None = None
    wait_timeout: int | None = None
    restore_after: bool = False


# ── templates / images ────────────────────────────────────────────────


class CreateTemplatesRequest(BaseModel):
    template_names: str | None = Field(None, description="csv of template keys (default: all)")
    force: bool = False


class ImportImageRequest(BaseModel):
    manifest_uri: str = Field(..., description="URI of the image manifest")
    vm_name: str | None = None
    vm_dir: str | None = None
    provider: str | None = None


class PushImageRequest(BaseModel):
    image_ref: str = Field(..., description="OCI image reference")
    qcow2: str = Field(..., description="path to the qcow2 disk image")
    metadata: str | None = None


# ── run / tasks ───────────────────────────────────────────────────────


class RunRequest(BaseModel):
    task_name: str | None = None
    cmd: str | None = None
    ansible_flags: str | None = None
    cluster: str | None = None


# ── netlab ────────────────────────────────────────────────────────────


class NetlabDestroyRequest(BaseModel):
    confirm: bool = Field(..., description="must be true — tears down the lab")
