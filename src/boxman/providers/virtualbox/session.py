"""
:class:`VirtualBoxSession` — the VirtualBox provider session.

This is the rewritten, first-class successor to the non-functional legacy
``boxman.virtualbox.vboxmanage.Virtualbox``. It structurally satisfies the
:class:`boxman.abstract.providers.ProviderSession` protocol (so
``isinstance(session, ProviderSession)`` is True and the manager can drive it
exactly like :class:`~boxman.providers.libvirt.session.LibVirtSession`).

Phase 1 status (skeleton / consolidation):

* **Real now** — the config surface (``provider_config`` / ``uri`` /
  ``use_sudo`` + setters, ``update_provider_config``) and a
  side-effect-free constructor. The legacy ``__init__`` eagerly shelled out
  (``vboxmanage natnetwork list``) and raised ``FileNotFoundError`` on any host
  without VirtualBox installed; this one touches nothing external.
* **No-op now** — ``update_provider_config_with_runtime`` (VirtualBox only
  supports the ``local`` runtime, so there is no runtime metadata to inject).
* **Stubbed now** — every per-VM / network / snapshot / storage operation the
  manager calls raises ``NotImplementedError`` with a "lands in Phase N"
  message, giving later phases a red-to-green target. Real provisioning is
  built on top of :mod:`boxman.providers.virtualbox.commands` and the helper
  modules in this package in subsequent phases.
"""

from __future__ import annotations

from typing import Any, NoReturn

from boxman import log


def _phase(method: str, phase: int) -> NotImplementedError:
    """Return the standard 'not-implemented-yet' error for a stubbed method."""
    return NotImplementedError(
        f"VirtualBox provider: {method} lands in Phase {phase}")


class VirtualBoxSession:
    """A live session against the VirtualBox (VBoxManage) backend."""

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Initialize the session.

        NOTE: construction is deliberately side-effect free — it must not shell
        out to ``VBoxManage`` (that was the core defect in the legacy session,
        and it is also required so the ``isinstance(x, ProviderSession)``
        protocol check works on any host).

        Args:
            config: Optional project configuration dictionary.
        """
        #: Optional[Dict[str, Any]]: the configuration for this session
        self.config = config or {}

        #: logging.Logger: the logger instance
        self.logger = log

        # provider config from the project configuration — these are
        # authoritative and must never be overridden by app-level defaults.
        self._project_provider_config = self.config.get('provider', {}).get('virtualbox', {})

        #: Dict[str, Any]: the base provider config (may be enriched with
        #: app-level settings, but project-level keys always win via the
        #: property getter).
        self._provider_config_base = self._project_provider_config.copy()

        #: the boxman manager instance (set by the CLI after construction)
        self.manager = None

    # --- config surface -----------------------------------------------------

    @property
    def provider_config(self) -> dict[str, Any]:
        """
        Return the effective provider config.

        Project-level settings (from conf.yml) always take precedence over
        app-level (boxman.yml) values.
        """
        merged = self._provider_config_base.copy()
        merged.update(self._project_provider_config)
        return merged

    @provider_config.setter
    def provider_config(self, value: dict[str, Any]) -> None:
        """Set the base provider config (project-level keys still win)."""
        self._provider_config_base = value

    @property
    def uri(self) -> str:
        """
        Connection URI.

        VirtualBox has no libvirt-style connection URI; this exists to satisfy
        the provider protocol and defaults to an empty string.
        """
        return self.provider_config.get('uri', '')

    @uri.setter
    def uri(self, value: str) -> None:
        self._provider_config_base['uri'] = value

    @property
    def use_sudo(self) -> bool:
        return self.provider_config.get('use_sudo', False)

    @use_sudo.setter
    def use_sudo(self, value: bool) -> None:
        self._provider_config_base['use_sudo'] = value

    def update_provider_config(self, new_config: dict[str, Any]) -> None:
        """
        Merge *new_config* into the base provider config; project-level
        settings always win (enforced by the property getter).

        Args:
            new_config: Additional config keys (e.g. from the boxman.yml
                ``providers`` section).
        """
        merged = self._provider_config_base.copy()
        merged.update(new_config)
        self._provider_config_base = merged

    def update_provider_config_with_runtime(self) -> None:
        """
        No-op for VirtualBox.

        VirtualBox only supports the ``local`` runtime (VBoxManage runs
        directly on the host), so there is no runtime metadata to inject into
        the provider config. Present so the manager can call it uniformly
        across providers.
        """
        return None

    # --- image import -------------------------------------------------------

    def import_image(self,
                     manifest_uri: str,
                     vm_name: str,
                     vm_dir: str,
                     manifest_local_path: str | None = None) -> bool:
        raise _phase('import_image', 5)

    # --- networking ---------------------------------------------------------

    def define_network(self,
                       name: str | None = None,
                       info: dict[str, Any] | None = None,
                       workdir: str | None = None) -> bool:
        raise _phase('define_network', 3)

    def destroy_network(self,
                        name: str | None = None,
                        info: dict[str, Any] | None = None) -> bool:
        raise _phase('destroy_network', 3)

    def undefine_network(self,
                         name: str | None = None,
                         info: dict[str, Any] | None = None) -> bool:
        raise _phase('undefine_network', 3)

    def remove_network(self,
                       name: str | None = None,
                       info: dict[str, Any] | None = None) -> bool:
        raise _phase('remove_network', 3)

    # --- VM lifecycle -------------------------------------------------------

    def clone_vm(self,
                 new_vm_name: str,
                 src_vm_name: str,
                 info: dict[str, Any],
                 workdir: str) -> bool:
        raise _phase('clone_vm', 2)

    def vm_exists(self, vm_name: str) -> bool:
        raise _phase('vm_exists', 2)

    def template_disks_present(self, vm_name: str) -> bool:
        raise _phase('template_disks_present', 2)

    def destroy_vm(self, name: str, force: bool = False) -> bool:
        raise _phase('destroy_vm', 2)

    def destroy_disks(self,
                      workdir: str,
                      vm_name: str,
                      disks: list[dict[str, str]]) -> bool:
        raise _phase('destroy_disks', 2)

    def start_vm(self, vm_name: str) -> bool:
        raise _phase('start_vm', 2)

    def set_boot_order(self, vm_name: str, order: list[str]) -> bool:
        raise _phase('set_boot_order', 2)

    def restore_boot_order(self, vm_name: str) -> bool:
        raise _phase('restore_boot_order', 2)

    def wait_for_ssh(self,
                     ip: str,
                     port: int = 22,
                     timeout: int = 600,
                     interval: int = 10) -> bool:
        raise _phase('wait_for_ssh', 2)

    def eject_cdrom(self, vm_name: str) -> None:
        raise _phase('eject_cdrom', 2)

    # --- VM configuration ---------------------------------------------------

    def configure_vm_cpu_memory(self,
                                vm_name: str,
                                cpus: dict[str, int] | None = None,
                                memory_mb: int | None = None,
                                max_vcpus: int | None = None,
                                max_memory_mb: int | None = None) -> bool:
        raise _phase('configure_vm_cpu_memory', 2)

    def configure_vm_network_interfaces(self,
                                        vm_name: str,
                                        network_adapters: list[dict[str, Any]]) -> bool:
        raise _phase('configure_vm_network_interfaces', 2)

    def configure_vm_disks(self,
                           vm_name: str,
                           disks: list[dict[str, Any]],
                           workdir: str,
                           disk_prefix: str = "") -> bool:
        raise _phase('configure_vm_disks', 2)

    def configure_vm_cdroms(self,
                            vm_name: str,
                            cdroms: list[dict[str, Any]]) -> bool:
        raise _phase('configure_vm_cdroms', 2)

    def configure_vm_shared_folders(self,
                                    vm_name: str,
                                    shared_folders: list[dict[str, Any]]) -> bool:
        raise _phase('configure_vm_shared_folders', 2)

    def get_vm_ip_addresses(self, vm_name: str) -> dict[str, str]:
        raise _phase('get_vm_ip_addresses', 2)

    # --- update operations (for `boxman update`) ----------------------------

    def shutdown_and_wait(self, vm_name: str, timeout: int = 60) -> bool:
        raise _phase('shutdown_and_wait', 2)

    def update_vm_cpu_memory(self,
                             vm_name: str,
                             cpus: dict[str, int] | None,
                             memory_mb: int | None,
                             vm_state: str,
                             actual_cpus: dict[str, int],
                             actual_memory_mb: int,
                             max_vcpus: int | None = None,
                             max_memory_mb: int | None = None) -> dict[str, Any]:
        raise _phase('update_vm_cpu_memory', 2)

    def update_vm_disks(self,
                        vm_name: str,
                        new_disks: list[dict[str, Any]],
                        resize_disks: list[dict[str, Any]],
                        workdir: str,
                        disk_prefix: str,
                        vm_running: bool) -> bool:
        raise _phase('update_vm_disks', 2)

    def update_vm_cdroms(self,
                         vm_name: str,
                         new_cdroms: list[dict[str, Any]],
                         removed_cdroms: list[dict[str, Any]],
                         changed_cdroms: list[dict[str, Any]],
                         vm_running: bool) -> bool:
        raise _phase('update_vm_cdroms', 2)

    def update_vm_shared_folders(self,
                                 vm_name: str,
                                 new_folders: list[dict[str, Any]],
                                 removed_folders: list[dict[str, Any]],
                                 changed_folders: list[dict[str, Any]],
                                 vm_running: bool) -> dict[str, Any]:
        raise _phase('update_vm_shared_folders', 2)

    # --- control ------------------------------------------------------------

    def suspend_vm(self, vm_name: str) -> bool:
        raise _phase('suspend_vm', 2)

    def resume_vm(self, vm_name: str) -> bool:
        raise _phase('resume_vm', 2)

    def save_vm(self, vm_name: str, workdir: str) -> bool:
        raise _phase('save_vm', 2)

    def restore_vm(self, vm_name: str, workdir: str) -> bool:
        raise _phase('restore_vm', 2)

    # --- snapshots ----------------------------------------------------------

    def snapshot_take(self, *args: Any, **kwargs: Any) -> bool:
        raise _phase('snapshot_take', 4)

    def snapshot_list(self, vm_name: str | None = None) -> list[dict[str, str]]:
        raise _phase('snapshot_list', 4)

    def snapshot_restore(self,
                         vm_name: str,
                         snapshot_name: str | None = None) -> bool:
        raise _phase('snapshot_restore', 4)

    def snapshot_delete(self, vm_name: str, snapshot_name: str) -> bool:
        raise _phase('snapshot_delete', 4)

    def snapshot_log_data(self, vm_name: str) -> dict:
        raise _phase('snapshot_log_data', 4)

    def get_latest_snapshot(self, vm_name: str) -> str | None:
        raise _phase('get_latest_snapshot', 4)

    def validate_snapshot(self,
                          vm_name: str,
                          snapshot_name: str) -> tuple[bool, list[str]]:
        raise _phase('validate_snapshot', 4)

    def compress_snapshots_memory(self,
                                  vm_name: str,
                                  level: int = 3,
                                  decompress: bool = False) -> tuple[int, int]:
        raise _phase('compress_snapshots_memory', 4)

    # --- storage ------------------------------------------------------------

    @property
    def storage(self) -> NoReturn:
        raise _phase('storage', 6)
