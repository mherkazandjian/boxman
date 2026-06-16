"""
Fast unit tests for the API shim — no FastAPI app, no libvirt, no broker.

Covers the three pure pieces that the whole layer rests on:
  - the Pydantic→argv builder (operations registry)
  - provider capability gating
  - RBAC role math
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ── argv shim ──────────────────────────────────────────────────────────


def test_build_argv_flags_and_csv():
    from boxman.api.operations import OPERATIONS, build_op_argv

    argv = build_op_argv(OPERATIONS["provision"], {"force": True, "rebuild_templates": False})
    assert argv == ["provision", "--force"]

    argv = build_op_argv(OPERATIONS["snapshot_take"], {"boxes": ["a", "b"], "name": "s1"})
    assert argv[:2] == ["snapshot", "take"]
    assert "--vms" in argv and "a,b" in argv
    assert "--name" in argv and "s1" in argv


def test_build_argv_bool_pair_live():
    from boxman.api.operations import OPERATIONS, build_op_argv

    assert "--live" in build_op_argv(OPERATIONS["snapshot_take"], {"live": True})
    assert "--no-live" in build_op_argv(OPERATIONS["snapshot_take"], {"live": False})
    # None → neither flag
    argv = build_op_argv(OPERATIONS["snapshot_take"], {"live": None})
    assert "--live" not in argv and "--no-live" not in argv


def test_build_argv_auto_accept_and_read_json():
    from boxman.api.operations import OPERATIONS, build_op_argv

    # destructive op injects -y
    assert build_op_argv(OPERATIONS["destroy"], {})[-1] == "-y"
    # read op appends --json
    assert build_op_argv(OPERATIONS["ps"], {})[-1] == "--json"


def test_build_argv_positional_after_flags():
    from boxman.api.operations import OPERATIONS, build_op_argv

    argv = build_op_argv(OPERATIONS["push_image"], {"image_ref": "r/x:1", "qcow2": "/d.qcow2"})
    assert argv[:2] == ["image", "push"]
    assert argv[-1] == "r/x:1"  # positional last
    assert "--qcow2" in argv


def test_build_full_argv_prepends_global_flags():
    from boxman.api.cli_runner import build_full_argv
    from boxman.api.operations import OPERATIONS

    argv = build_full_argv(
        OPERATIONS["provision"], {"force": True},
        conf_path="/p/conf.yml", runtime="docker-compose",
    )
    assert "--conf" in argv and "/p/conf.yml" in argv
    # docker-compose runtime maps to the CLI's "docker" choice
    assert argv[argv.index("--runtime") + 1] == "docker"
    # global flags precede the subcommand token
    assert argv.index("--conf") < argv.index("provision")


def test_build_full_argv_local_runtime_omits_flag():
    from boxman.api.cli_runner import build_full_argv
    from boxman.api.operations import OPERATIONS

    argv = build_full_argv(OPERATIONS["up"], {}, conf_path="/p/conf.yml", runtime="local")
    assert "--runtime" not in argv


def test_every_operation_builds_without_error():
    from boxman.api.operations import OPERATIONS, build_op_argv

    for name, op in OPERATIONS.items():
        argv = build_op_argv(op, {})
        assert argv[: len(op.subcommand)] == list(op.subcommand), name


# ── capabilities ────────────────────────────────────────────────────────


def test_capabilities_provider_gating():
    from boxman.api.capabilities import caps_for, supports, universal_caps

    assert supports("libvirt", "storage.qcow2")
    assert not supports("virtualbox", "storage.qcow2")
    assert supports("virtualbox", "snapshot")
    # unknown provider only gets agnostic caps
    assert caps_for("xen") == {"meta", "run"}
    # universal caps are the intersection (+ agnostic)
    u = universal_caps()
    assert "lifecycle" in u and "snapshot" in u
    assert "storage.qcow2" not in u and "netlab" not in u


# ── RBAC math ────────────────────────────────────────────────────────────


def test_role_rank_and_global_role():
    from boxman.api.auth import rbac
    from boxman.api.db.models import Role, User

    assert rbac.rank("admin") > rbac.rank("operator") > rbac.rank("viewer")

    op_user = User(username="o", hashed_password="x", role=Role.operator.value)
    assert rbac.has_global_role(op_user, "viewer")
    assert rbac.has_global_role(op_user, "operator")
    assert not rbac.has_global_role(op_user, "admin")
    assert rbac.is_admin(User(username="a", hashed_password="x", role=Role.admin.value))


# ── secret redaction ──────────────────────────────────────────────────────


def test_redact_masks_sensitive_keys():
    from boxman.api.redact import redact

    out = redact({"vm": "node1", "password": "hunter2", "api_token": "abc", "force": True})
    assert out["vm"] == "node1" and out["force"] is True
    assert out["password"] != "hunter2" and out["api_token"] != "abc"


def test_jobdetail_redacts_params():
    from boxman.api.schemas.jobs import JobDetail

    detail = JobDetail(id="1", state="completed", operation="run_task",
                       params={"cmd": "x", "secret": "s3cret"})
    assert detail.params["secret"] == "***redacted***"
    assert detail.params["cmd"] == "x"
