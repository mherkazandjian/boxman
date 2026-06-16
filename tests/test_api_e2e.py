"""
End-to-end API test against a *running* stack (api + worker + redis + libvirt).

Marked ``integration`` so it is excluded from the default run. Drives a real
provision → status → snapshot → destroy cycle through the HTTP API. It is
gated on ``BOXMAN_API_E2E_URL`` and skips cleanly when the stack/box isn't
configured, so it never fails a machine that isn't set up for it.

Environment:
    BOXMAN_API_E2E_URL       base URL of the running API (e.g. http://localhost:8080)
    BOXMAN_API_E2E_USER      admin username (default: admin)
    BOXMAN_API_E2E_PASSWORD  admin password (required)
    BOXMAN_API_E2E_PROJECT   project name to register (default: e2e)
    BOXMAN_API_E2E_CONF      path (on the API host) to a tiny project's conf.yml
    BOXMAN_API_E2E_RUNTIME   runtime for the project (default: docker-compose)
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

BASE = os.environ.get("BOXMAN_API_E2E_URL")
PASSWORD = os.environ.get("BOXMAN_API_E2E_PASSWORD")
CONF = os.environ.get("BOXMAN_API_E2E_CONF")


def _require_stack():
    if not BASE or not PASSWORD or not CONF:
        pytest.skip("BOXMAN_API_E2E_URL / _PASSWORD / _CONF not set")
    httpx = pytest.importorskip("httpx")
    try:
        httpx.get(f"{BASE}/healthz", timeout=5)
    except Exception:  # noqa: BLE001
        pytest.skip(f"API at {BASE} not reachable")
    return httpx


def _wait_job(httpx, headers, job_id, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = httpx.get(f"{BASE}/jobs/{job_id}", headers=headers, timeout=30).json()
        if job["state"] in ("completed", "failed", "canceled"):
            return job
        time.sleep(5)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_provision_snapshot_destroy_cycle():
    httpx = _require_stack()
    user = os.environ.get("BOXMAN_API_E2E_USER", "admin")
    project = os.environ.get("BOXMAN_API_E2E_PROJECT", "e2e")
    runtime = os.environ.get("BOXMAN_API_E2E_RUNTIME", "docker-compose")

    token = httpx.post(
        f"{BASE}/auth/token", data={"username": user, "password": PASSWORD}, timeout=30
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    # register (idempotent-ish: ignore 409 if already there)
    httpx.post(f"{BASE}/projects",
               json={"name": project, "conf": CONF, "runtime": runtime},
               headers=h, timeout=30)

    # capabilities should report a provider
    caps = httpx.get(f"{BASE}/projects/{project}/capabilities", headers=h, timeout=30).json()
    assert "provider" in caps and "lifecycle" in caps["caps"]

    # provision → wait
    job_id = httpx.post(f"{BASE}/projects/{project}/provision", json={"force": True},
                        headers=h, timeout=30).json()["id"]
    job = _wait_job(httpx, h, job_id)
    assert job["state"] == "completed", httpx.get(
        f"{BASE}/jobs/{job_id}/log", headers=h).text[-2000:]

    # status reflects the provisioned boxes
    status = httpx.get(f"{BASE}/projects/{project}/status", headers=h, timeout=30).json()
    assert status["boxes"], "expected at least one box after provision"

    # snapshot take → list
    snap_job = httpx.post(f"{BASE}/projects/{project}/snapshots",
                          json={"boxes": "all", "name": "e2e-snap"}, headers=h, timeout=30).json()
    _wait_job(httpx, h, snap_job["id"])

    # teardown
    destroy_job = httpx.post(f"{BASE}/projects/{project}/destroy",
                             json={"confirm": True}, headers=h, timeout=30).json()
    assert _wait_job(httpx, h, destroy_job["id"])["state"] == "completed"
