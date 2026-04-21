"""
Exception hierarchy for boxman.

A small, purpose-built hierarchy that lets callers distinguish between
config errors, provisioning failures, and transient runtime issues
without matching on exception messages. Added in Phase 2.2 of the
review plan (see /home/mher/.claude/plans/).

Callers should prefer these over bare ``Exception`` so failure classes
can be handled differently (e.g. retry transient runtime failures but
surface config errors to the user immediately). Always chain the
original error with ``raise ProvisionError(...) from exc`` so the cause
remains visible in tracebacks.
"""

from __future__ import annotations


class BoxmanError(Exception):
    """Base class for every exception raised by boxman internals."""


class ConfigError(BoxmanError):
    """Raised for problems in ``boxman.yml`` / ``conf.yml`` — missing
    required fields, unresolvable ``${env:VAR}`` placeholders, bad YAML."""


class ProvisionError(BoxmanError):
    """Raised when a provisioning step fails (clone, disk setup, network
    definition, SSH injection). Subclass further for granular handling."""


class NetworkError(ProvisionError):
    """Raised when a libvirt network cannot be created, destroyed, or
    inspected. Includes bridge collisions and missing NAT config."""


class TemplateError(ProvisionError):
    """Raised when a base-image template cannot be built — missing image,
    cloud-init seed ISO failure, or virt-install refusing the spec."""


class SnapshotError(BoxmanError):
    """Raised when snapshot take / restore / delete fails. Distinguishes
    between "snapshot doesn't exist" and "snapshot revert aborted" so
    callers can decide whether to retry."""


class RuntimeUnavailable(BoxmanError):
    """Raised when the selected runtime (docker-compose, local libvirt)
    is not reachable — docker daemon down, libvirtd not responding, or
    the runtime container refused to start. Typically retriable."""
