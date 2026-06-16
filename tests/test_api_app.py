"""
App-level API tests: auth, RBAC, lifecycle jobs, gating — all with the boxman
CLI exec and redis lock stubbed and Celery in eager mode, so no FastAPI-external
systems (libvirt, redis) are needed.
"""

from __future__ import annotations

import contextlib
import json

import pytest

pytestmark = pytest.mark.unit


def _fake_cli_result(op, payload=None, **_kw):
    """Canned CliResult standing in for a real `boxman <cmd>` invocation."""
    from boxman.api.cli_runner import CliResult

    sub = tuple(op.subcommand)
    if sub == ("ps",):
        out = json.dumps([{"vm": "node1", "state": "running", "cluster": "c1"}])
    elif sub == ("snapshot", "log"):
        out = json.dumps({"node1": [{"name": "s1"}]})
    elif sub == ("conf",):
        out = json.dumps({"provider": {"libvirt": {"uri": "qemu:///system"}}})
    elif sub == ("storage", "df"):
        out = "node1  10G  used 3G\n"
    else:
        out = "{}"
    return CliResult(argv=["boxman", *sub], returncode=0, stdout=out, stderr="")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOXMAN_API_DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
    monkeypatch.setenv("BOXMAN_API_CACHE_FILE", str(tmp_path / "projects.json"))
    monkeypatch.setenv("BOXMAN_API_JOB_LOG_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("BOXMAN_API_BOOTSTRAP_ADMIN_PASSWORD", "adminpw")

    # reset cached settings + db engine so the temp env takes effect
    from boxman.api import config as cfg
    from boxman.api.db import session as dbs

    cfg.get_settings.cache_clear()
    dbs._engine = None
    dbs._SessionLocal = None

    # Celery eager + stub the heavy bits
    from boxman.api.jobs import celery_app as ca
    from boxman.api.jobs import tasks

    ca.celery_app.conf.task_always_eager = True

    def fake_stream(op, payload, log_path, **_kw):
        import os

        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as fobj:
            fobj.write(f"ran {' '.join(op.subcommand)}\n")
        return 0

    monkeypatch.setattr(tasks, "stream_to_file", fake_stream)
    monkeypatch.setattr(tasks, "project_lock", lambda p, *a, **k: contextlib.nullcontext())

    # stub the synchronous read path (no real boxman/libvirt)
    import boxman.api.deps as deps

    monkeypatch.setattr(deps, "run_sync", _fake_cli_result)

    from fastapi.testclient import TestClient

    from boxman.api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _token(client, username, password):
    resp = client.post("/auth/token", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, token, name="demo", conf="/tmp/conf.yml"):
    return client.post("/projects", json={"name": name, "conf": conf, "runtime": "local"},
                       headers=_h(token))


# ── auth ────────────────────────────────────────────────────────────────


def test_unauthenticated_is_401(client):
    assert client.get("/projects").status_code == 401


def test_login_and_me(client):
    token = _token(client, "admin", "adminpw")
    me = client.get("/auth/me", headers=_h(token)).json()
    assert me["username"] == "admin" and me["role"] == "admin"


def test_bad_password_is_401(client):
    assert client.post("/auth/token", data={"username": "admin", "password": "no"}).status_code == 401


# ── RBAC ──────────────────────────────────────────────────────────────────


def test_viewer_cannot_mutate_but_can_read(client, tmp_path):
    admin = _token(client, "admin", "adminpw")
    _register(client, admin, conf=str(tmp_path / "conf.yml"))

    vic = client.post("/auth/users", json={"username": "vic", "password": "p", "role": "viewer"},
                      headers=_h(admin)).json()
    client.post(f"/auth/users/{vic['id']}/grants", json={"project": "demo", "role": "viewer"},
                headers=_h(admin))
    vt = _token(client, "vic", "p")

    # viewer can read status (stubbed ps)
    assert client.get("/projects/demo/status", headers=_h(vt)).status_code == 200
    # viewer cannot provision
    assert client.post("/projects/demo/provision", json={}, headers=_h(vt)).status_code == 403


def test_ungranted_user_cannot_see_project(client, tmp_path):
    admin = _token(client, "admin", "adminpw")
    _register(client, admin, conf=str(tmp_path / "conf.yml"))
    nob = client.post("/auth/users", json={"username": "nob", "password": "p", "role": "viewer"},
                      headers=_h(admin)).json()
    nt = _token(client, "nob", "p")
    assert client.get("/projects/demo", headers=_h(nt)).status_code == 403
    assert client.get("/projects", headers=_h(nt)).json() == []  # filtered out


# ── lifecycle jobs ────────────────────────────────────────────────────────


def test_provision_job_completes_and_logs(client, tmp_path):
    admin = _token(client, "admin", "adminpw")
    _register(client, admin, conf=str(tmp_path / "conf.yml"))

    resp = client.post("/projects/demo/provision", json={"force": True}, headers=_h(admin))
    assert resp.status_code == 202
    job_id = resp.json()["id"]

    job = client.get(f"/jobs/{job_id}", headers=_h(admin)).json()
    assert job["state"] == "completed" and job["exit_code"] == 0
    log = client.get(f"/jobs/{job_id}/log", headers=_h(admin)).text
    assert "ran provision" in log


def test_destroy_requires_confirm(client, tmp_path):
    admin = _token(client, "admin", "adminpw")
    _register(client, admin, conf=str(tmp_path / "conf.yml"))
    assert client.post("/projects/demo/destroy", json={"confirm": False},
                       headers=_h(admin)).status_code == 400
    assert client.post("/projects/demo/destroy", json={"confirm": True},
                       headers=_h(admin)).status_code == 202


def test_active_job_conflicts(client, tmp_path, monkeypatch):
    admin = _token(client, "admin", "adminpw")
    _register(client, admin, conf=str(tmp_path / "conf.yml"))

    # Make the task a no-op so the first job stays 'pending' (active).
    from boxman.api.jobs import tasks

    class _NoRun:
        def delay(self, *_a, **_k):
            return None

    monkeypatch.setattr(tasks, "run_operation", _NoRun())

    first = client.post("/projects/demo/up", json={}, headers=_h(admin))
    assert first.status_code == 202
    second = client.post("/projects/demo/up", json={}, headers=_h(admin))
    assert second.status_code == 409


# ── capability gating ─────────────────────────────────────────────────────


def test_storage_compact_gated_on_virtualbox(client, tmp_path, monkeypatch):
    admin = _token(client, "admin", "adminpw")
    _register(client, admin, conf=str(tmp_path / "conf.yml"))

    import boxman.api.deps as deps

    monkeypatch.setattr(deps, "detect_provider", lambda entry: "virtualbox")
    resp = client.post("/projects/demo/storage/compact", json={"method": "sparsify"},
                       headers=_h(admin))
    assert resp.status_code == 501
    assert "storage.qcow2" in resp.json()["detail"]
