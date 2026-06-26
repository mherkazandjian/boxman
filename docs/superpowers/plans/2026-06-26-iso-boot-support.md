# ISO Boot Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ISO-boot VM creation to boxman so VMs can boot directly from an ISO file (e.g. Talos Linux / Omni), with a named `isos:` top-level config section for DRY URL+checksum declarations shared across many VMs.

**Architecture:** A new `IsoBootVM` class (parallel to `BareVM`) handles `virt-install --cdrom <iso> --boot cdrom,hd`. The `isos:` config section declares named ISOs with `uri:` + `checksum:`; boxman downloads and caches them via the existing `ImageCache` before provisioning. Manager resolves `cdroms: [{name: foo}]` references to local paths and injects `_resolved_iso_path` into `vm_info` before the parallel clone subprocesses start. Dispatch in `session.clone_vm()` checks `boot_order[0] == 'cdrom'` exactly as the existing PXE path checks `'network'`.

**Tech Stack:** Python 3.12, libvirt/KVM, `virt-install`, `qemu-img`, existing `ImageCache` + `_shell_run` + `VirshCommand`/`VirtInstallCommand` helpers.

## Global Constraints

- Follow the `BareVM` class shape exactly — same `__init__` signature pattern, same `_shell_run` import, same `_wrap_for_runtime` calls
- Tests use `pytest.mark.unit`, `MagicMock`, `@patch` — match `tests/test_bare_vm.py` style
- `_resolved_iso_path` is a private key injected into `vm_info` dicts — never written to conf.yml, only in-memory
- All new methods on `BoxmanManager` are instance methods (not `@staticmethod` or `@classmethod`)
- ImageCache default dir is `~/.cache/boxman/images` — ISOs share the same cache as cloud images

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| CREATE | `src/boxman/providers/libvirt/iso_boot_vm.py` | `IsoBootVM` class — empty disk + virt-install --cdrom |
| CREATE | `tests/test_iso_boot_vm.py` | Unit tests for `IsoBootVM` |
| MODIFY | `src/boxman/manager.py` | `_resolve_isos()`, `_inject_resolved_iso()`, `_download_iso()`, fix `validate_base_images()`, wire into `clone_vms()` and `_clone_and_configure_new_vms()` |
| CREATE | `tests/test_iso_resolution.py` | Unit tests for ISO resolution methods |
| MODIFY | `src/boxman/providers/libvirt/session.py` | Add `boot_order[0] == 'cdrom'` branch in `clone_vm()` |
| CREATE | `boxes/talos-iso-boot/conf.yml` | Example box demonstrating ISO boot |

---

## Task 1: `IsoBootVM` class

**Files:**
- Create: `src/boxman/providers/libvirt/iso_boot_vm.py`
- Create: `tests/test_iso_boot_vm.py`

**Interfaces:**
- Produces: `IsoBootVM(vm_name, info, provider_config, workdir, iso_path).create() -> bool`
- `iso_path` is a pre-resolved local file path (caller's responsibility to download)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_iso_boot_vm.py`:

```python
"""Unit tests for boxman.providers.libvirt.iso_boot_vm.IsoBootVM."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.iso_boot_vm import IsoBootVM

pytestmark = pytest.mark.unit


def _result(ok: bool = True, stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.ok = ok
    r.stderr = stderr
    r.failed = not ok
    return r


def _make_iso_vm(tmp_path: Path, iso_path: str = "/fake/talos.iso", **info_overrides) -> IsoBootVM:
    info = dict(
        memory=2048,
        vcpus=2,
        disks=[{"name": "disk01", "size": 20}],
        networks=[{"name": "default"}],
    )
    info.update(info_overrides)
    return IsoBootVM(
        vm_name="iso-test01",
        info=info,
        provider_config={"use_sudo": False, "uri": "qemu:///system"},
        workdir=str(tmp_path),
        iso_path=iso_path,
    )


class TestGetDiskSizeGb:
    def test_returns_first_disk_size(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disks=[{"name": "disk01", "size": 50}])
        assert vm._get_disk_size_gb() == 50

    def test_defaults_to_20_when_no_disks(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disks=[])
        assert vm._get_disk_size_gb() == 20

    def test_defaults_to_20_when_size_missing(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disks=[{"name": "disk01"}])
        assert vm._get_disk_size_gb() == 20


class TestGetNetwork:
    def test_returns_first_network_name(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[{"name": "talos-net"}])
        assert vm._get_network() == "talos-net"

    def test_defaults_to_default_when_no_networks(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[])
        assert vm._get_network() == "default"

    def test_defaults_to_default_when_name_missing(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[{}])
        assert vm._get_network() == "default"


class TestIsoBootVMCreate:
    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_create_success(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        assert vm.create() is True
        assert mock_run.call_count == 2

    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_virt_install_cmd_contains_cdrom_and_boot(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        vm.create()
        virt_install_call = mock_run.call_args_list[1][0][0]
        assert "--cdrom=/data/talos.iso" in virt_install_call
        assert "--boot=cdrom,hd" in virt_install_call

    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_create_fails_on_qemu_img_error(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=False, stderr="no space left")
        vm = _make_iso_vm(tmp_path)
        assert vm.create() is False
        assert mock_run.call_count == 1

    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_create_fails_on_virt_install_error(self, mock_run, tmp_path):
        mock_run.side_effect = [_result(ok=True), _result(ok=False, stderr="permission denied")]
        vm = _make_iso_vm(tmp_path)
        assert vm.create() is False
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/orski/git/boxman
python -m pytest tests/test_iso_boot_vm.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'boxman.providers.libvirt.iso_boot_vm'`

- [ ] **Step 3: Create `src/boxman/providers/libvirt/iso_boot_vm.py`**

```python
"""Create a VM that boots directly from an ISO (e.g. Talos Linux)."""

import os
from typing import Any

from boxman import log
from boxman.utils.shell import run as _shell_run

from .commands import VirshCommand, VirtInstallCommand


class IsoBootVM:
    """
    Create a libvirt VM with an empty disk and a CDROM ISO attached,
    boot order set to [cdrom, hd].

    Intended for OSes that live-boot and self-install from an ISO
    (e.g. Talos Linux via Omni). The iso_path must already be resolved
    to a local file by the caller.
    """

    def __init__(
        self,
        vm_name: str,
        info: dict[str, Any],
        provider_config: dict[str, Any],
        workdir: str,
        iso_path: str,
    ):
        self.vm_name = vm_name
        self.info = info
        self.provider_config = provider_config
        self.workdir = workdir
        self.iso_path = iso_path
        self.logger = log
        self.virsh = VirshCommand(provider_config)
        self.virt_install = VirtInstallCommand(provider_config=provider_config)

    def create(self) -> bool:
        """Create the ISO-boot VM."""
        disk_path = os.path.join(self.workdir, f"{self.vm_name}.qcow2")
        disk_size = self._get_disk_size_gb()
        memory = self.info.get("memory", 2048)
        vcpus = self.info.get("vcpus", 2)
        network = self._get_network()

        qemu_img_cmd = f'qemu-img create -f qcow2 "{disk_path}" {disk_size}G'
        qemu_img_cmd = self.virsh._wrap_for_runtime(qemu_img_cmd)
        result = _shell_run(qemu_img_cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"qemu-img create failed: {result.stderr}")
            return False

        parts = []
        if self.virt_install.use_sudo:
            parts.append("sudo")
        parts.append(self.virt_install.command_path)
        parts.append(f"--connect={self.virt_install.uri}")
        parts.append(f"--name={self.vm_name}")
        parts.append(f"--memory={memory}")
        parts.append(f"--vcpus={vcpus}")
        parts.append(f"--disk=path={disk_path},format=qcow2,bus=virtio,discard=unmap")
        parts.append(f"--network=network={network},model=virtio")
        parts.append(f"--cdrom={self.iso_path}")
        parts.append("--boot=cdrom,hd")
        parts.append("--os-variant=detect=on,require=off")
        parts.append("--graphics=vnc")
        parts.append("--noautoconsole")
        parts.append("--wait=0")

        cmd = " ".join(parts)
        cmd = self.virt_install._wrap_for_runtime(cmd)
        self.logger.info(f"creating ISO-boot VM '{self.vm_name}': {cmd}")
        result = _shell_run(cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"virt-install failed: {result.stderr}")
            return False

        self.logger.info(f"ISO-boot VM '{self.vm_name}' created")
        return True

    def _get_disk_size_gb(self) -> int:
        disks = self.info.get("disks", [{}])
        return disks[0].get("size", 20) if disks else 20

    def _get_network(self) -> str:
        networks = self.info.get("networks", [{}])
        return networks[0].get("name", "default") if networks else "default"
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_iso_boot_vm.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/boxman/providers/libvirt/iso_boot_vm.py tests/test_iso_boot_vm.py
git commit -m "feat(iso-boot): add IsoBootVM class and unit tests"
```

---

## Task 2: ISO resolution methods on `BoxmanManager`

**Files:**
- Modify: `src/boxman/manager.py` — add `_download_iso()`, `_resolve_isos()`, `_inject_resolved_iso()`
- Create: `tests/test_iso_resolution.py`

**Interfaces:**
- Consumes: `ImageCache` from `boxman.image_cache`
- Produces:
  - `BoxmanManager._download_iso(url: str, dst_path: str) -> bool`
  - `BoxmanManager._resolve_isos() -> dict[str, str]`  maps iso_name → local_path
  - `BoxmanManager._inject_resolved_iso(vm_info: dict, resolved_isos: dict[str, str]) -> dict`  returns updated vm_info copy with `_resolved_iso_path` injected when `boot_order[0] == 'cdrom'`

- [ ] **Step 1: Write failing tests**

Create `tests/test_iso_resolution.py`:

```python
"""Unit tests for BoxmanManager ISO resolution helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


def _manager_with_config(config: dict) -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = config
    mgr.app_config = {}
    mgr.logger = MagicMock()
    return mgr


class TestResolveIsos:
    def test_returns_empty_dict_when_no_isos_section(self):
        mgr = _manager_with_config({})
        assert mgr._resolve_isos() == {}

    def test_returns_empty_dict_when_isos_is_empty(self):
        mgr = _manager_with_config({"isos": {}})
        assert mgr._resolve_isos() == {}

    def test_raises_when_uri_missing(self):
        mgr = _manager_with_config({"isos": {"talos-omni": {}}})
        with pytest.raises(ValueError, match="missing 'uri'"):
            mgr._resolve_isos()

    def test_calls_image_cache_ensure(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = "/cache/talos.iso"
            mock_cache_cls.from_config.return_value = mock_cache
            result = mgr._resolve_isos()
        assert result == {"talos-omni": "/cache/talos.iso"}
        mock_cache.ensure.assert_called_once_with(
            "https://example.com/talos.iso", mgr._download_iso
        )

    def test_raises_when_download_fails(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = None
            mock_cache_cls.from_config.return_value = mock_cache
            with pytest.raises(RuntimeError, match="Failed to download ISO"):
                mgr._resolve_isos()

    def test_verifies_checksum_when_provided(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = "/cache/talos.iso"
            mock_cache.verify_checksum.return_value = True
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=True)
            result = mgr._resolve_isos()
        assert result == {"talos-omni": "/cache/talos.iso"}

    def test_raises_on_checksum_mismatch(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = "/cache/talos.iso"
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=False)
            with pytest.raises(RuntimeError, match="Checksum mismatch"):
                mgr._resolve_isos()


class TestInjectResolvedIso:
    def test_resolves_named_cdrom_reference(self):
        mgr = _manager_with_config({})
        vm_info = {
            "boot_order": ["cdrom", "hd"],
            "cdroms": [{"name": "talos-omni"}],
        }
        resolved_isos = {"talos-omni": "/cache/talos.iso"}
        result = mgr._inject_resolved_iso(vm_info, resolved_isos)
        assert result["cdroms"][0]["source"] == "/cache/talos.iso"
        assert result["_resolved_iso_path"] == "/cache/talos.iso"

    def test_does_not_mutate_input_vm_info(self):
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["cdrom", "hd"], "cdroms": [{"name": "talos-omni"}]}
        resolved_isos = {"talos-omni": "/cache/talos.iso"}
        mgr._inject_resolved_iso(vm_info, resolved_isos)
        assert "_resolved_iso_path" not in vm_info

    def test_raises_on_unknown_named_iso(self):
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["cdrom", "hd"], "cdroms": [{"name": "unknown-iso"}]}
        with pytest.raises(ValueError, match="unknown iso 'unknown-iso'"):
            mgr._inject_resolved_iso(vm_info, {})

    def test_no_injection_when_boot_order_is_not_cdrom(self):
        mgr = _manager_with_config({})
        vm_info = {
            "boot_order": ["hd"],
            "cdroms": [{"name": "talos-omni"}],
        }
        resolved_isos = {"talos-omni": "/cache/talos.iso"}
        result = mgr._inject_resolved_iso(vm_info, resolved_isos)
        assert "_resolved_iso_path" not in result

    def test_passthrough_when_no_cdroms(self):
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["hd"]}
        result = mgr._inject_resolved_iso(vm_info, {})
        assert result == vm_info

    def test_inline_source_string_passes_through_unchanged(self):
        mgr = _manager_with_config({})
        vm_info = {
            "boot_order": ["cdrom", "hd"],
            "cdroms": [{"source": "/local/talos.iso"}],
        }
        result = mgr._inject_resolved_iso(vm_info, {})
        assert result["cdroms"][0]["source"] == "/local/talos.iso"
        assert result["_resolved_iso_path"] == "/local/talos.iso"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_iso_resolution.py -v 2>&1 | head -20
```

Expected: `AttributeError: type object 'BoxmanManager' has no attribute '_resolve_isos'`

- [ ] **Step 3: Add `_download_iso`, `_resolve_isos`, `_inject_resolved_iso` to `manager.py`**

Find the end of `_create_templates_impl` (around line 1165) and add these three methods immediately after. Add `from boxman.image_cache import ImageCache` to the top-level imports in `manager.py` (it's currently imported inline; add it at the top so it's available module-wide).

First, add to the top-level imports section of `manager.py` (near the other `from boxman...` imports):

```python
from boxman.image_cache import ImageCache
```

Then add these three methods to the `BoxmanManager` class. Find the line `def ensure_templates_exist(self)` (around line 1351) and insert before it:

```python
    def _download_iso(self, url: str, dst_path: str) -> bool:
        """Download an ISO from a URL, trying wget then curl."""
        from boxman.utils.shell import run as _shell_run
        self.logger.info(f"downloading ISO {url} -> {dst_path}")
        result = _shell_run(
            f'wget --progress=dot:mega -O "{dst_path}" "{url}"',
            hide=False, warn=True,
        )
        if result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
            self.logger.info("ISO download complete (wget)")
            return True
        result = _shell_run(
            f'curl -L --progress-bar -o "{dst_path}" "{url}"',
            hide=False, warn=True,
        )
        if result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
            self.logger.info("ISO download complete (curl)")
            return True
        self.logger.error(f"failed to download ISO from {url}")
        return False

    def _resolve_isos(self) -> dict[str, str]:
        """Download and cache all ISOs declared in the ``isos:`` config section.

        Returns a mapping of iso_name -> local_file_path.
        """
        isos_conf = self.config.get("isos", {}) if self.config else {}
        if not isos_conf:
            return {}

        cache_conf = (self.app_config or {}).get("cache", {})
        cache = ImageCache.from_config(cache_conf)

        resolved: dict[str, str] = {}
        for name, iso_conf in isos_conf.items():
            uri = iso_conf.get("uri")
            if not uri:
                raise ValueError(f"iso '{name}' missing 'uri'")
            checksum = iso_conf.get("checksum")

            local_path = cache.ensure(uri, self._download_iso)
            if local_path is None:
                raise RuntimeError(
                    f"Failed to download ISO '{name}' from {uri}"
                )

            if checksum and not ImageCache.verify_checksum(local_path, checksum):
                raise RuntimeError(f"Checksum mismatch for ISO '{name}'")

            resolved[name] = local_path

        return resolved

    def _inject_resolved_iso(
        self, vm_info: dict, resolved_isos: dict[str, str]
    ) -> dict:
        """Resolve ``cdroms:`` name references and inject ``_resolved_iso_path``.

        Returns a shallow copy of vm_info with:
        - Each cdrom entry using ``name:`` expanded to ``source: <local_path>``
        - ``_resolved_iso_path`` set to ``cdroms[0]['source']`` when
          ``boot_order[0] == 'cdrom'``
        """
        cdroms = vm_info.get("cdroms", [])
        if not cdroms:
            return vm_info

        resolved_cdroms = []
        for cdrom in cdroms:
            if "name" in cdrom:
                iso_name = cdrom["name"]
                if iso_name not in resolved_isos:
                    raise ValueError(
                        f"cdroms references unknown iso '{iso_name}'. "
                        f"Declare it in the 'isos:' section."
                    )
                resolved_cdroms.append({**cdrom, "source": resolved_isos[iso_name]})
            else:
                resolved_cdroms.append(cdrom)

        result = {**vm_info, "cdroms": resolved_cdroms}

        boot_order = vm_info.get("boot_order", ["hd"])
        if boot_order and boot_order[0] == "cdrom" and resolved_cdroms:
            first_source = resolved_cdroms[0].get("source")
            if first_source:
                result["_resolved_iso_path"] = first_source

        return result
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_iso_resolution.py -v
```

Expected: all 11 tests PASS.

- [ ] **Step 5: Run the full test suite to catch regressions**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/boxman/manager.py tests/test_iso_resolution.py
git commit -m "feat(iso-boot): add _resolve_isos and _inject_resolved_iso to BoxmanManager"
```

---

## Task 3: Fix `validate_base_images` and wire ISO resolution into `clone_vms`

**Files:**
- Modify: `src/boxman/manager.py` — `validate_base_images()` (~line 1333), `clone_vms()` (~line 1760), `_clone_and_configure_new_vms()` (~line 4512)

**Interfaces:**
- Consumes: `_resolve_isos() -> dict[str, str]`, `_inject_resolved_iso(vm_info, resolved_isos) -> dict` from Task 2

- [ ] **Step 1: Fix `validate_base_images` to skip ISO-boot and PXE VMs**

Find the method at ~line 1333. Replace:

```python
        for vm_name, vm_info in cluster.get('vms', {}).items():
                if not vm_info.get('base_image') and not cluster_base:
                    missing.append(f"{cluster_name}.vms.{vm_name}")
```

With:

```python
        for vm_name, vm_info in cluster.get('vms', {}).items():
                boot_order = vm_info.get('boot_order', ['hd'])
                if boot_order and boot_order[0] in ('network', 'cdrom'):
                    continue
                if not vm_info.get('base_image') and not cluster_base:
                    missing.append(f"{cluster_name}.vms.{vm_name}")
```

- [ ] **Step 2: Wire `_resolve_isos` and `_inject_resolved_iso` into `clone_vms`**

In `clone_vms()` (~line 1760), find the line:

```python
        clone_tasks = list(vm_clone_tasks())
```

Replace it with:

```python
        resolved_isos = self._resolve_isos()
        clone_tasks = [
            (cluster, self._inject_resolved_iso(vm_info, resolved_isos), new_vm_name)
            for cluster, vm_info, new_vm_name in vm_clone_tasks()
        ]
```

- [ ] **Step 3: Fix the `base_image` check in `clone_vms._clone` (~line 1799)**

Replace:

```python
                    src_vm_name = vm_info.get('base_image') or cluster.get('base_image')
                    if not src_vm_name:
                        raise ValueError(
                            f"no base_image for VM '{new_vm_name}': "
                            f"set base_image at the cluster or VM level"
                        )
```

With:

```python
                    src_vm_name = vm_info.get('base_image') or cluster.get('base_image')
                    boot_order = vm_info.get('boot_order', ['hd'])
                    if not src_vm_name and not (boot_order and boot_order[0] in ('network', 'cdrom')):
                        raise ValueError(
                            f"no base_image for VM '{new_vm_name}': "
                            f"set base_image at the cluster or VM level"
                        )
```

- [ ] **Step 4: Apply the same two fixes to `_clone_and_configure_new_vms`**

In `_clone_and_configure_new_vms()` (~line 4512), find the `clone_tasks = []` line (after the storage pool loop) and replace the block that builds `clone_tasks` with ISO injection:

Find:
```python
        clone_tasks = []
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full = f"{prj_name}_{cluster_name}_{vm_name}"
```

The `clone_tasks.append(...)` call is a few lines down. Add ISO resolution before the loop:

```python
        resolved_isos = self._resolve_isos()
        clone_tasks = []
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full = f"{prj_name}_{cluster_name}_{vm_name}"
```

Then find where `clone_tasks.append((cluster, vm_info.copy(), full))` is called (~line 4573) and change it to:

```python
                clone_tasks.append((cluster, self._inject_resolved_iso(vm_info.copy(), resolved_isos), full))
```

Also apply the same `base_image` check fix to `_clone_and_configure_new_vms._clone` (~line 4541):

```python
                    src_vm_name = vm_info.get('base_image') or cluster.get('base_image')
                    boot_order = vm_info.get('boot_order', ['hd'])
                    if not src_vm_name and not (boot_order and boot_order[0] in ('network', 'cdrom')):
                        raise ValueError(
                            f"no base_image for VM '{new_vm_name}': "
                            f"set base_image at the cluster or VM level"
                        )
```

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/boxman/manager.py
git commit -m "feat(iso-boot): wire ISO resolution into provision and update flows, fix validate_base_images"
```

---

## Task 4: Dispatch `boot_order: [cdrom, hd]` in `session.clone_vm`

**Files:**
- Modify: `src/boxman/providers/libvirt/session.py` (~line 234)

**Interfaces:**
- Consumes: `IsoBootVM(vm_name, info, provider_config, workdir, iso_path).create() -> bool` from Task 1
- Consumes: `info['_resolved_iso_path']` injected by Task 2/3

- [ ] **Step 1: Write a failing test for the new dispatch branch**

Add to `tests/test_libvirt_clone_vm.py` (or create `tests/test_iso_boot_dispatch.py` if the existing file is too large):

```python
# Add this test class to tests/test_libvirt_clone_vm.py

class TestCloneVmIsoBootDispatch:
    """clone_vm delegates to IsoBootVM when boot_order starts with 'cdrom'."""

    def _make_session(self):
        from boxman.providers.libvirt.session import LibVirtSession
        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {"uri": "qemu:///system", "use_sudo": False}
        return session

    @patch("boxman.providers.libvirt.session.IsoBootVM")
    def test_cdrom_boot_order_dispatches_to_iso_boot_vm(self, mock_iso_cls, tmp_path):
        mock_iso_cls.return_value.create.return_value = True
        session = self._make_session()
        info = {
            "boot_order": ["cdrom", "hd"],
            "_resolved_iso_path": "/cache/talos.iso",
        }
        result = session.clone_vm(
            new_vm_name="cp-01",
            src_vm_name=None,
            info=info,
            workdir=str(tmp_path),
        )
        assert result is True
        mock_iso_cls.assert_called_once_with(
            vm_name="cp-01",
            info=info,
            provider_config=session.provider_config,
            workdir=str(tmp_path),
            iso_path="/cache/talos.iso",
        )
        mock_iso_cls.return_value.create.assert_called_once()

    @patch("boxman.providers.libvirt.session.IsoBootVM")
    def test_raises_when_resolved_iso_path_missing(self, mock_iso_cls, tmp_path):
        session = self._make_session()
        info = {"boot_order": ["cdrom", "hd"]}
        with pytest.raises(RuntimeError, match="_resolved_iso_path"):
            session.clone_vm(
                new_vm_name="cp-01",
                src_vm_name=None,
                info=info,
                workdir=str(tmp_path),
            )

    @patch("boxman.providers.libvirt.session.IsoBootVM")
    def test_raises_when_iso_boot_vm_create_fails(self, mock_iso_cls, tmp_path):
        mock_iso_cls.return_value.create.return_value = False
        session = self._make_session()
        info = {"boot_order": ["cdrom", "hd"], "_resolved_iso_path": "/cache/talos.iso"}
        with pytest.raises(RuntimeError, match="Failed to create ISO-boot VM"):
            session.clone_vm(
                new_vm_name="cp-01",
                src_vm_name=None,
                info=info,
                workdir=str(tmp_path),
            )
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
python -m pytest tests/test_libvirt_clone_vm.py::TestCloneVmIsoBootDispatch -v 2>&1 | head -20
```

Expected: tests fail (no `IsoBootVM` import in session.py yet).

- [ ] **Step 3: Add the `cdrom` dispatch branch to `session.clone_vm`**

In `src/boxman/providers/libvirt/session.py`, find the `clone_vm` method (~line 234). After the existing `if boot_order and boot_order[0] == 'network':` block (which ends around line 266), insert before the `cloner = CloneVM(...)` line:

```python
        if boot_order and boot_order[0] == 'cdrom':
            from .iso_boot_vm import IsoBootVM
            iso_path = info.get('_resolved_iso_path')
            if not iso_path:
                raise RuntimeError(
                    f"boot_order is [cdrom, hd] but '_resolved_iso_path' is not set "
                    f"in info for VM '{new_vm_name}'. Ensure the vm's cdroms: entry "
                    f"references a named iso from the isos: section."
                )
            iso_vm = IsoBootVM(
                vm_name=new_vm_name,
                info=info,
                provider_config=self.provider_config,
                workdir=workdir,
                iso_path=iso_path,
            )
            status = iso_vm.create()
            if not status:
                raise RuntimeError(
                    f"Failed to create ISO-boot VM '{new_vm_name}'"
                )
            return True
```

Also update the docstring of `clone_vm` to mention the new path:

```python
        """
        Clone a VM, create a bare PXE-boot VM, or create an ISO-boot VM,
        depending on boot_order[0]: 'network' → BareVM, 'cdrom' → IsoBootVM,
        anything else → CloneVM.
        ...
        """
```

- [ ] **Step 4: Run the new tests to confirm they pass**

```bash
python -m pytest tests/test_libvirt_clone_vm.py::TestCloneVmIsoBootDispatch -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/boxman/providers/libvirt/session.py
git commit -m "feat(iso-boot): dispatch boot_order=[cdrom,hd] to IsoBootVM in session.clone_vm"
```

---

## Task 5: Example box and hpc-k8s-infra conf.yml

**Files:**
- Create: `boxes/talos-iso-boot/conf.yml`
- Modify: `/home/orski/git/hpc-k8s-infra/boxman/sc1-talos-cluster/conf.yml`

- [ ] **Step 1: Create `boxes/talos-iso-boot/conf.yml`**

```yaml
# Example: 3-node Talos Linux cluster booted from ISO
#
# Boot flow:
#   1. boxman downloads and caches the ISO (once, shared by all VMs)
#   2. Each VM starts with the ISO as CDROM, boot order: cdrom then hd
#   3. Talos live-boots from ISO, registers with Omni
#   4. Omni pushes machine config; Talos installs to disk and reboots
#
# Replace the ISO uri/checksum with the actual Omni ISO for your instance.
# Download via: omnictl download iso --output /tmp/omni-talos.iso
#
version: '1.0'
project: boxman_dev_talos-iso-boot

provider:
  libvirt:
    uri: qemu:///system
    use_sudo: true
    virt_install_cmd: '/bin/python3 /usr/bin/virt-install'
    virt_clone_cmd: '/bin/python3 /usr/bin/virt-clone'
    virsh_cmd: '/usr/bin/virsh'

isos:
  talos-omni:
    uri: https://factory.talos.dev/image/talos-omni-placeholder.iso
    checksum: sha256:0000000000000000000000000000000000000000000000000000000000000000

workspace:
  path: ~/workspaces/talos-iso-boot

clusters:
  talos:
    workdir: ~/workspaces/talos-iso-boot
    networks:
      talos-net:
        type: nat
        cidr: 192.168.100.0/24
        dhcp: true
    vms:
      cp-01:
        vcpus: 2
        memory: 4096
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 50
        networks:
          - name: talos-net
        cdroms:
          - name: talos-omni
      cp-02:
        vcpus: 2
        memory: 4096
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 50
        networks:
          - name: talos-net
        cdroms:
          - name: talos-omni
      cp-03:
        vcpus: 2
        memory: 4096
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 50
        networks:
          - name: talos-net
        cdroms:
          - name: talos-omni
```

- [ ] **Step 2: Update `hpc-k8s-infra/boxman/sc1-talos-cluster/conf.yml`**

Replace the stub file with the real VM specs:

```yaml
# boxman cluster configuration for sc1-talos
# Run with: cd boxman/sc1-talos-cluster && boxman up
version: '1.0'
project: sc1-talos

provider:
  libvirt:
    uri: qemu:///system
    use_sudo: true
    virt_install_cmd: '/bin/python3 /usr/bin/virt-install'
    virt_clone_cmd: '/bin/python3 /usr/bin/virt-clone'
    virsh_cmd: '/usr/bin/virsh'

isos:
  talos-omni:
    uri: https://omni.example.sc1/omni-talos.iso
    checksum: sha256:<replace-after-omnictl-download>
    # Download: omnictl download iso --output ~/omni-talos.iso

workspace:
  path: /mnt/data/sc1-talos

clusters:
  sc1-talos:
    workdir: /mnt/data/sc1-talos
    networks:
      sc1-talos-net:
        type: nat
        cidr: 192.168.20.0/24
        dhcp: true
    vms:
      cp-01:
        vcpus: 2
        memory: 4096
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 50
        networks:
          - name: sc1-talos-net
        cdroms:
          - name: talos-omni

      cp-02:
        vcpus: 2
        memory: 4096
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 50
        networks:
          - name: sc1-talos-net
        cdroms:
          - name: talos-omni

      cp-03:
        vcpus: 2
        memory: 4096
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 50
        networks:
          - name: sc1-talos-net
        cdroms:
          - name: talos-omni

      worker-01:
        vcpus: 4
        memory: 6144
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 150
        networks:
          - name: sc1-talos-net
        cdroms:
          - name: talos-omni

      worker-02:
        vcpus: 4
        memory: 6144
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 150
        networks:
          - name: sc1-talos-net
        cdroms:
          - name: talos-omni

      worker-03:
        vcpus: 4
        memory: 6144
        boot_order: [cdrom, hd]
        disks:
          - name: disk01
            size: 150
        networks:
          - name: sc1-talos-net
        cdroms:
          - name: talos-omni

      mgmt-01:
        vcpus: 2
        memory: 8192
        base_image: ubuntu-24.04-minimal-base-template-cloudinit
        disks:
          - name: disk01
            size: 200
        networks:
          - name: sc1-talos-net
```

- [ ] **Step 3: Run full boxman test suite one final time**

```bash
cd /home/orski/git/boxman
python -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 4: Commit boxman example box**

```bash
git add boxes/talos-iso-boot/conf.yml
git commit -m "feat(iso-boot): add talos-iso-boot example box"
```

- [ ] **Step 5: Commit hpc-k8s-infra conf.yml** (in the other repo)

```bash
cd /home/orski/git/hpc-k8s-infra
git add boxman/sc1-talos-cluster/conf.yml
git commit -m "feat(sc1): fill in sc1-talos-cluster conf.yml with real VM specs"
```
