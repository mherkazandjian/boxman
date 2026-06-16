"""
Boxman HTTP API.

A FastAPI layer that exposes boxman's functionality over HTTP. The API is
purely *additive* — it drives the existing ``boxman`` CLI as a subprocess
(see :mod:`boxman.api.cli_runner`) rather than reaching into the manager or
provider internals, so the boxman core stays untouched and the surface stays
provider-agnostic (libvirt today, virtualbox / containers later).

Long-running and mutating operations are dispatched to Celery workers; fast
read/status operations run synchronously off the request thread. See the
design plan for the full rationale.
"""

__all__ = ["create_app"]


def create_app(*args, **kwargs):  # pragma: no cover - thin re-export
    """Lazy re-export of :func:`boxman.api.main.create_app`.

    Imported lazily so that merely importing :mod:`boxman.api` (e.g. for the
    operations registry in tests) does not require FastAPI to be installed.
    """
    from boxman.api.main import create_app as _create_app

    return _create_app(*args, **kwargs)
