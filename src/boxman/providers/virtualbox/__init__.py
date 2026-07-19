"""
VirtualBox provider package.

Mirrors the layout of :mod:`boxman.providers.libvirt`: a
:class:`~boxman.providers.virtualbox.session.VirtualBoxSession` that satisfies
the :class:`boxman.abstract.providers.ProviderSession` protocol, a fixed
``VBoxManage`` command runner in :mod:`~boxman.providers.virtualbox.commands`,
and per-concern command-builder helper modules (clone_vm, destroy_vm, net,
snapshot, vm_info, storage, modifyvm).
"""

from __future__ import annotations

from .session import VirtualBoxSession

__all__ = ['VirtualBoxSession']
