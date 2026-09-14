"""
Executed behaviour tests for boxes/proxmox-nested-ceph-cluster's Makefile.

`make -n` proves a recipe expands and `bash -n` proves it parses. Neither can
see an exit status being discarded, a credential written from a failed run, or
a plaintext secret reaching a host -- which is how six defects survived a round
of review with both checks green (#171). Everything here runs `make` for real,
with stub executables on PATH, and asserts what actually happened.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess

import pytest

pytestmark = pytest.mark.unit

BOX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "boxes", "proxmox-nested-ceph-cluster")

PASSWORD = "s3cret-under-test"


def _stub(path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def box(tmp_path):
    """A throwaway copy of the box with its own key, and a stub bin/ on PATH."""
    dst = tmp_path / "repo" / "boxes" / "proxmox-nested-ceph-cluster"
    dst.parent.mkdir(parents=True)
    shutil.copytree(BOX, dst, ignore=shutil.ignore_patterns(
        "keys", "answer.toml", "*.iso", "conf.rendered.yml", ".terraform",
        "*.tfstate*", ".env", "terraform.tfvars"))
    (dst / "keys").mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test",
                    "-f", str(dst / "keys" / "id_ed25519_pvelab")], check=True)
    (tmp_path / "bin").mkdir()
    return dst


def _make(box, *targets, env=None, **overrides):
    args = ["make", "-C", str(box), *targets]
    args += [f"{k}={v}" for k, v in overrides.items()]
    e = dict(os.environ, PATH=f"{box.parent.parent.parent / 'bin'}:{os.environ['PATH']}")
    e.update(env or {})
    return subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)


# ── tf-token: a token must never come from a run that failed (B7, B8) ────────

def test_a_token_from_a_failed_run_is_refused(box, tmp_path):
    """ssh emitting a token and *then* failing must not be accepted.

    pipefail makes the pipeline's status visible, but the status of the
    assignment was never checked, so make exited 0 and replaced the
    credentials with a token from a failed run (#171 B7).
    """
    tf = tmp_path / "tf"; tf.mkdir()
    (tf / ".env").write_text("export TF_VAR_pve_api_token='existing'\n")
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, 'echo \'{"value":"leaked-token"}\'\nexit 255\n')

    r = _make(box, "tf-token", SSH=str(ssh), TF_DIR=str(tf))

    assert r.returncode != 0, f"a failed token run reported success:\n{r.stdout}"
    assert (tf / ".env").read_text() == "export TF_VAR_pve_api_token='existing'\n", \
        "existing credentials were replaced from a failed run"


def test_a_run_returning_no_token_is_refused(box, tmp_path):
    tf = tmp_path / "tf"; tf.mkdir()
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, 'echo "no json here"\n')

    r = _make(box, "tf-token", SSH=str(ssh), TF_DIR=str(tf))

    assert r.returncode != 0
    assert not (tf / ".env").exists()


def test_an_accepted_token_lands_0600(box, tmp_path):
    """The token is root@pam with --privsep 0 (#171 B8)."""
    tf = tmp_path / "tf"; tf.mkdir()
    (tf / ".env").write_text("stale\n")
    os.chmod(tf / ".env", 0o644)
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, 'echo \'{"value":"good-token"}\'\n')

    r = _make(box, "tf-token", SSH=str(ssh), TF_DIR=str(tf))

    assert r.returncode == 0, r.stdout + r.stderr
    assert "good-token" in (tf / ".env").read_text()
    assert stat.S_IMODE((tf / ".env").stat().st_mode) == 0o600, \
        "an existing permissive file was not repaired"


# ── terraform must not run on the ambient environment (B9) ───────────────────

@pytest.mark.parametrize("content,why", [
    ("export TF_VAR_pve_api_token='unterminated\n", "unterminated quote"),
    # `exit 42` ends the recipe's shell outright, so it fails with or without
    # the fix. `false` as the last command makes `.` return non-zero while the
    # shell survives -- which is the case the check is actually for.
    ("export TF_VAR_pve_endpoint='https://x/'\nfalse\n", "non-zero return"),
])
def test_terraform_refuses_an_env_that_cannot_be_sourced(box, tmp_path, content, why):
    """`test -r` proves the file opens, not that it parsed (#171 B9)."""
    tf = tmp_path / "tf"; tf.mkdir()
    (tf / ".env").write_text(content)
    terraform = tmp_path / "bin" / "terraform"
    _stub(terraform, f'echo "$@" >> {tmp_path}/terraform-calls\n')

    r = _make(box, "tf-plan", TF_DIR=str(tf), TF=str(terraform))

    assert r.returncode != 0, f"terraform ran despite a {why} in .env"
    assert not (tmp_path / "terraform-calls").exists(), \
        f"terraform was invoked with a {why} in .env"


# ── the root password must not be left on disk (B15) ─────────────────────────

def test_rendering_the_answer_file_leaves_no_password_behind(box):
    """The stamp that recorded the password was 0644 and not rsync-excluded,
    so `make sync` shipped the plaintext root password to both hosts -- the
    very trap B14 is about (#171 B15)."""
    r = _make(box, "answer", PVE_ROOT_PASSWORD=PASSWORD)
    assert r.returncode == 0, r.stdout + r.stderr

    holding = []
    for root, _dirs, files in os.walk(box):
        for f in files:
            p = os.path.join(root, f)
            if os.path.basename(p) in ("answer.toml", "Makefile"):
                continue
            try:
                if PASSWORD in open(p, encoding="utf-8", errors="ignore").read():
                    holding.append(os.path.relpath(p, box))
            except OSError:
                pass
    assert not holding, f"the plaintext password was written to {holding}"


def _hash_is_for(answer_toml: str, password: str) -> bool:
    """Whether the rendered SHA-512 crypt hash is the one for *password*.

    Comparing rendered bytes proves nothing: `openssl passwd -6` picks a random
    salt, so two renders of the *same* password already differ. Re-deriving the
    hash with the salt it actually used is the only check that discriminates.
    """
    m = re.search(r"\$6\$[A-Za-z0-9./]+\$[A-Za-z0-9./]+", answer_toml)
    assert m, f"no sha512-crypt hash in the rendered answer file:\n{answer_toml}"
    full = m.group(0)
    salt = full.split("$")[2]
    out = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, password],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip() == full


def test_the_rendered_hash_is_the_one_for_the_requested_password(box):
    """#171 B15. The earlier version compared file bytes across two renders,
    which differ whatever the password is."""
    _make(box, "answer", PVE_ROOT_PASSWORD="first-one")
    assert _hash_is_for((box / "answer.toml").read_text(), "first-one")

    _make(box, "answer", PVE_ROOT_PASSWORD="second-one")
    rendered = (box / "answer.toml").read_text()
    assert _hash_is_for(rendered, "second-one"), \
        "the password override was a no-op; the ISO would install the old one"
    assert not _hash_is_for(rendered, "first-one")


# ── host-clean must not call a failed teardown clean (B18) ───────────────────

def test_host_clean_refuses_to_report_clean_after_a_failed_removal(box, tmp_path):
    """The removals sat in `&&` lists whose status nothing inspected, so the
    script printed "host <site> clean" having removed nothing (#171 B18)."""
    fake = tmp_path / "bin"
    _stub(fake / "ip", 'exit 1\n')          # no bridge, no vxlan
    _stub(fake / "bridge", 'exit 0\n')
    _stub(fake / "sudo", '''
shift_args=("$@")
if [[ "${shift_args[0]}" == firewall-cmd ]]; then
    for a in "$@"; do
        [[ $a == --query-* ]] && exit 0      # "it is bound"
        [[ $a == --remove-* ]] && exit 1     # ...but removal fails
    done
fi
exit 0
''')
    env = dict(PATH=f"{fake}:{os.environ['PATH']}", BOXMAN_SITE="hpe2",
               PVE_LAB_DIR=str(tmp_path / "lab"))
    r = subprocess.run(["bash", str(box / "scripts" / "host-clean.sh")],
                       capture_output=True, text=True, env=dict(os.environ, **env),
                       timeout=60)

    assert r.returncode != 0, f"a failed teardown reported success:\n{r.stdout}"
    assert "clean" not in r.stdout.split("ERROR")[-1], r.stdout


# ── the failover drill: victims as data, and a verdict that survives (B12/B13) ─

#: The watcher polls until every victim is `started` somewhere other than the
#: dead node, so a stub that keeps reporting them on it never terminates.
#: `on` selects which node the snapshot puts them on.
def _ha_stub_lib(on: str) -> str:
    return (
        'set -euo pipefail\n'
        'NODES=(pve1 pve2 pve3 pve4)\n'
        'declare -A NODE_IP=([pve1]=10.77.0.11 [pve2]=10.77.0.12 '
        '[pve3]=10.77.0.13 [pve4]=10.77.0.14)\n'
        'LOGS=$PWD; log() { :; }; die() { echo "die $*"; exit 1; }; sleep() { :; }\n'
        'pssh() {\n'
        '  printf \'[{"type":"node","node":"pve4","status":"offline"},\'\n'
        f'  printf \'{{"type":"service","node":"{on}","sid":"vm:200","state":"started"}},\'\n'
        f'  printf \'{{"type":"service","node":"{on}","sid":"vm:201","state":"started"}}]\'\n'
        '}\n')


def _ha_watch(box, tmp_path, *args, on="pve1"):
    work = tmp_path / f"ha-{on}-{len(args)}"
    work.mkdir(exist_ok=True)
    shutil.copy(box / "scripts" / "ha-watch.sh", work / "ha-watch.sh")
    (work / "lib.sh").write_text(_ha_stub_lib(on))
    return subprocess.run(["bash", "./ha-watch.sh", *args], cwd=work,
                          capture_output=True, text=True, timeout=30)


def test_the_victim_inventory_is_listed_one_per_line(box, tmp_path):
    """--list takes the snapshot before the kill, so it reads the node the
    services are still on (#171 B13)."""
    r = _ha_watch(box, tmp_path, "pve4", "--list", on="pve4")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.split() == ["vm:200", "vm:201"]


@pytest.mark.parametrize("victims", [[], ["vm:200"], ["vm:200", "vm:201"]])
def test_preset_victims_are_accepted_as_arguments(box, tmp_path, victims):
    """They cross a remote shell as words. Passed with their newlines intact,
    everything after the first became a command of its own (#171 B13)."""
    r = _ha_watch(box, tmp_path, "pve4", "30", *victims)
    assert "not a usable HA resource id" not in r.stdout + r.stderr, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_sid_that_is_not_a_sid_is_refused(box, tmp_path):
    r = _ha_watch(box, tmp_path, "pve4", "30", "vm:200 ; rm -rf /")
    assert r.returncode != 0
    assert "not a usable HA resource id" in r.stdout + r.stderr


def test_the_failover_drill_reports_a_failed_readiness_check(box, tmp_path):
    """`${restore_rc:-1}` substitutes only for unset or empty, so with
    restore_rc already 0 a failed readiness check was swallowed (#171 B12)."""
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, """
for a in "$@"; do
  case "$a" in
    *--list*)          echo "vm:200"; exit 0 ;;
    *wait-first-boot*) exit 42 ;;
    *ha-watch.sh*)     exit 0 ;;
  esac
done
exit 0
""")
    r = _make(box, "ha-failover", SSH=str(ssh), ORCH="hpe1", NODE="pve4")
    assert r.returncode != 0, (
        f"a node that never became ready reported success:\n{r.stdout}\n{r.stderr}")


# ── HA placement policy comes from terraform, not from arithmetic (D1) ───────

_HA_STUB_LIB = r"""
set -euo pipefail
NODES=(pve1 pve2 pve3 pve4)
declare -A NODE_SITE=([pve1]=hpe1 [pve2]=hpe1 [pve3]=hpe2 [pve4]=hpe2)
LOGS=$PWD
CALLS="$PWD/calls"; : > "$CALLS"
log() { :; }
die() { echo "die $*" >> "$CALLS"; exit 1; }
join_by() { local IFS="$1"; shift; echo "$*"; }
pssh() {
    local node=$1; shift; local cmd="$*"
    echo "$cmd" >> "$CALLS"
    case "$cmd" in
        *"/cluster/ha/resources"*) echo "$HA_RESOURCES" ;;
        *"/cluster/ha/rules"*)
            if [ "${RULE_EXISTS:-0}" = 1 ]; then
                echo '[{"rule":"prefer-hpe1","resources":"vm:200"},{"rule":"prefer-hpe2","resources":"vm:999"}]'
            else
                echo '[]'
            fi
            return 0 ;;
        *"/cluster/options"*)      echo '{"crs":"ha=dynamic"}' ;;
    esac
    return 0
}
"""


def _run_ha(box, tmp_path, homes=None, resources=None, rule_exists=False, tag="x"):
    work = tmp_path / f"ha-policy-{tag}"
    work.mkdir(exist_ok=True)
    shutil.copy(box / "scripts" / "pve-ha.sh", work / "pve-ha.sh")
    (work / "lib.sh").write_text(_HA_STUB_LIB)
    res = resources if resources is not None else [200, 201, 202, 203]
    env = dict(os.environ,
               HA_RESOURCES=str([{"sid": f"vm:{i}"} for i in res]).replace("'", '"'),
               RULE_EXISTS="1" if rule_exists else "0")
    if homes is not None:
        env["PVE_HA_POLICY_HOMES"] = homes
    else:
        env.pop("PVE_HA_POLICY_HOMES", None)
    proc = subprocess.run(["bash", "./pve-ha.sh"], cwd=work, env=env,
                          capture_output=True, text=True, timeout=60)
    calls = (work / "calls")
    return proc, calls.read_text().splitlines() if calls.exists() else []


def test_the_policy_is_required_with_no_fallback(box, tmp_path):
    """Guessing and warning would preserve the defect: the arithmetic could
    silently disagree with the placement terraform applied (#171 D1)."""
    proc, calls = _run_ha(box, tmp_path, homes=None, tag="missing")
    assert proc.returncode != 0
    assert not [c for c in calls if "/cluster/options" in c], \
        "CRS was changed before the policy was validated"


@pytest.mark.parametrize("homes,why", [
    ("vm:200", "no '=' separator"),
    ("200=pve1", "not a vm resource id"),
    ("vm:200=pve9", "unknown node"),
    ("", "empty"),
])
def test_a_malformed_policy_is_refused_before_anything_is_written(
        box, tmp_path, homes, why):
    proc, calls = _run_ha(box, tmp_path, homes=homes, tag=why.split()[0])
    assert proc.returncode != 0, why
    assert not [c for c in calls if "pvesh set" in c or "pvesh create" in c], \
        f"something was written despite {why}"


def test_placement_follows_the_policy_not_the_vm_id_arithmetic(box, tmp_path):
    """The discriminating case. `(200 - 200) % 4 == 0` puts vm:200 on hpe1 by
    the old rule; an override placing it on pve3 must win."""
    proc, calls = _run_ha(
        box, tmp_path, resources=[200],
        homes="vm:200=pve3", tag="override")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rules = [c for c in calls if "pvesh create" in c or "pvesh set /cluster/ha/rules" in c]
    assert any("prefer-hpe2" in c and "vm:200" in c for c in rules), rules
    assert not any("prefer-hpe1" in c and "vm:200" in c for c in rules), rules


def test_an_emptied_group_loses_its_rule(box, tmp_path):
    """A group with no members must not keep the membership it had last time."""
    proc, calls = _run_ha(box, tmp_path, resources=[200, 201],
                          homes="vm:200=pve1,vm:201=pve2",
                          rule_exists=True, tag="empty")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("pvesh delete /cluster/ha/rules/prefer-hpe2" in c for c in calls), calls


def test_a_resource_outside_the_policy_is_left_alone(box, tmp_path):
    """Not filed under whichever group the arithmetic would have picked."""
    proc, calls = _run_ha(box, tmp_path, resources=[200, 999],
                          homes="vm:200=pve1", tag="unowned")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rules = [c for c in calls if "/cluster/ha/rules" in c]
    assert not any("vm:999" in c for c in rules), rules


def test_multiple_victims_cross_the_remote_shell_as_arguments(box, tmp_path):
    """#171 B13, the Makefile half.

    `ha-watch.sh --list` prints one sid per line. Interpolated into the remote
    command string with the newlines intact, everything after the first became
    a command of its own. The script-side tests could not see this: they are
    handed clean argv by pytest.
    """
    log = tmp_path / "ssh-argv.log"
    ssh = tmp_path / "bin" / "ssh-stub"
    # One line per argument, so quoting the whole list into a single argument
    # is visible. `"$*"` joins them and cannot tell the two apart -- which is
    # how an earlier version of this test passed with the list quoted.
    _stub(ssh, f'''
printf 'ARGC=%s\\n' "$#" >> {log}
for a in "$@"; do printf 'ARG=%s\\n' "$a" >> {log}; done
for a in "$@"; do
  case "$a" in
    *--list*)          printf 'vm:200\\nvm:201\\n'; exit 0 ;;
    *wait-first-boot*) exit 0 ;;
    *ha-watch.sh*)     exit 0 ;;
  esac
done
exit 0
''')
    _make(box, "ha-failover", SSH=str(ssh), ORCH="hpe1", NODE="pve4")

    args = [ln[4:] for ln in log.read_text().splitlines() if ln.startswith("ARG=")]
    watch = [a for a in args if "ha-watch.sh" in a and "--list" not in a]
    assert watch, args

    # The remote command is one ssh argument; the sids have to arrive inside it
    # as separate *words*, neither glued together by quoting nor split across
    # lines. Split it the way the remote shell would.
    words = watch[0].split()
    assert "vm:200" in words and "vm:201" in words, (
        f"the sids did not arrive as separate words: {watch[0]!r}")
    assert not any(w.startswith("vm:200 ") or " vm:201" in w for w in words), (
        f"the victim list was quoted into a single argument: {watch[0]!r}")
    assert "\n" not in watch[0], (
        f"a newline survived into the remote command: {watch[0]!r}")


# ── HA rules, against a stub that actually keeps state (D1 follow-ups) ───────

#: Unlike the first stub, this one *retains* rule membership and enforces the
#: feasibility check Proxmox performs: a node-affinity rule is refused if one
#: of its resources already belongs to another. Without that, a rule update
#: that added a member before its old rule released it looked fine.
_HA_STATEFUL_LIB = r"""
set -euo pipefail
NODES=(pve1 pve2 pve3 pve4)
declare -A NODE_SITE=([pve1]=hpe1 [pve2]=hpe1 [pve3]=hpe2 [pve4]=hpe2)
LOGS=$PWD
CALLS="$PWD/calls"; : > "$CALLS"
RULES="$PWD/rules"; mkdir -p "$RULES"
log() { :; }
die() { echo "die $*" >> "$CALLS"; exit 1; }
join_by() { local IFS="$1"; shift; echo "$*"; }

_members_elsewhere() {   # _members_elsewhere <rule> <csv>
    local self=$1 csv=$2 other r m
    for other in "$RULES"/*; do
        [ -e "$other" ] || continue
        r=$(basename "$other"); [ "$r" = "$self" ] && continue
        for m in ${csv//,/ }; do
            grep -qw -- "$m" "$other" && return 0
        done
    done
    return 1
}

pssh() {
    local node=$1; shift; local cmd="$*"
    echo "$cmd" >> "$CALLS"
    case "$cmd" in
        *"/cluster/ha/resources"*)
            [ "${INVENTORY_FAILS:-0}" = 1 ] && { echo "$HA_RESOURCES"; return 255; }
            echo "$HA_RESOURCES"; return 0 ;;
        *"pvesh get /cluster/ha/rules --output-format json"*|*"pvesh get /cluster/ha/rules"*)
            [ "${RULE_LIST_FAILS:-0}" = 1 ] && { echo "[]"; return 255; }
            local out="[" first_e=1 r
            for r in "$RULES"/*; do
                [ -e "$r" ] || continue
                [ $first_e -eq 1 ] || out="$out,"; first_e=0
                out="$out{\"rule\":\"$(basename "$r")\",\"resources\":\"$(cat "$r")\"}"
            done
            echo "$out]"; return 0 ;;
        *"pvesh set /cluster/ha/rules/"*|*"pvesh create /cluster/ha/rules"*)
            local rule res
            if [[ $cmd == *"pvesh set"* ]]; then
                rule=${cmd##*/cluster/ha/rules/}; rule=${rule%% *}
            else
                rule=$(sed -n "s/.*--rule \([^ ]*\).*/\1/p" <<<"$cmd")
            fi
            res=$(sed -n "s/.*--resources '\([^']*\)'.*/\1/p" <<<"$cmd")
            if _members_elsewhere "$rule" "$res"; then
                echo "rule $rule not feasible: resource already in another rule" >&2
                return 1
            fi
            printf '%s' "$res" > "$RULES/$rule"; return 0 ;;
        *"pvesh delete /cluster/ha/rules/"*)
            local rule=${cmd##*/cluster/ha/rules/}; rule=${rule%% *}
            rm -f "$RULES/$rule"; return 0 ;;
        *"/cluster/options"*) echo '{"crs":"ha=dynamic"}'; return 0 ;;
    esac
    return 0
}
"""


def _run_ha_stateful(box, tmp_path, homes, resources, preset=None, tag="s", **env_extra):
    work = tmp_path / f"ha-stateful-{tag}"
    work.mkdir(exist_ok=True)
    shutil.copy(box / "scripts" / "pve-ha.sh", work / "pve-ha.sh")
    (work / "lib.sh").write_text(_HA_STATEFUL_LIB)
    (work / "rules").mkdir(exist_ok=True)
    for rule, members in (preset or {}).items():
        (work / "rules" / rule).write_text(members)
    env = dict(os.environ, PVE_HA_POLICY_HOMES=homes,
               HA_RESOURCES=str([{"sid": f"vm:{i}"} for i in resources]).replace("'", '"'),
               **{k: str(v) for k, v in env_extra.items()})
    proc = subprocess.run(["bash", "./pve-ha.sh"], cwd=work, env=env,
                          capture_output=True, text=True, timeout=60)
    rules = {p.name: p.read_text() for p in (work / "rules").iterdir()}
    return proc, rules


def test_a_resource_can_move_from_one_hosts_rule_to_the_others(box, tmp_path):
    """Proxmox refuses a rule whose resource is already in another one, so the
    additions have to wait for the withdrawals. The reverse direction happened
    to work, which is why one-directional testing missed it (#171 D1)."""
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve1,vm:201=pve1", resources=[200, 201],
        preset={"prefer-hpe1": "vm:200", "prefer-hpe2": "vm:201"}, tag="move")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "vm:201" in rules.get("prefer-hpe1", ""), rules
    assert "vm:201" not in rules.get("prefer-hpe2", ""), rules


def test_a_simultaneous_swap_is_applied(box, tmp_path):
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve3,vm:201=pve1", resources=[200, 201],
        preset={"prefer-hpe1": "vm:200", "prefer-hpe2": "vm:201"}, tag="swap")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rules.get("prefer-hpe1", "") == "vm:201", rules
    assert rules.get("prefer-hpe2", "") == "vm:200", rules


def test_a_failed_inventory_does_not_delete_a_rule(box, tmp_path):
    """`mapfile < <(cmd)` hides the failure even under pipefail, so a partial
    list was used as if complete and the cleanup deleted a live rule."""
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve1,vm:201=pve3", resources=[200],
        preset={"prefer-hpe2": "vm:201"}, tag="inv", INVENTORY_FAILS=1)

    assert proc.returncode != 0, proc.stdout
    assert "prefer-hpe2" in rules, "a rule was deleted on the strength of a failed query"
    # ...and nothing else was written either. Setting CRS before reading the
    # inventory left the cluster reconfigured but unreconciled.
    calls = (tmp_path / "ha-stateful-inv" / "calls").read_text()
    assert "pvesh set /cluster/options" not in calls, (
        "CRS was changed before the inventory had been read")


def test_a_failed_rule_listing_stops_the_run(box, tmp_path):
    """Absence and failure are indistinguishable in a per-rule GET: the
    upstream handler dies for both, and the status it produces is not a stable
    discriminator (absence surfaces as 255, a query failure can be 13). So the
    rules are listed once and *that* command's status is checked; a failure
    must stop the run rather than be read as "there are no rules"."""
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve1", resources=[200],
        preset={"prefer-hpe2": "vm:201"}, tag="list", RULE_LIST_FAILS=1)

    assert proc.returncode != 0, (
        f"a failed rule listing was read as 'no rules':\n{proc.stdout}")
    assert "prefer-hpe2" in rules, "a stale rule was deleted on a failed listing"
    calls = (tmp_path / "ha-stateful-list" / "calls").read_text()
    assert "pvesh set /cluster/options" not in calls, \
        "CRS was changed despite the failed listing"


def test_make_ha_refuses_a_failed_terraform_output(box, tmp_path):
    """Terraform emitting valid JSON and then failing still triggered the
    remote HA command; make exited 0 (#171 D1 follow-up)."""
    tf = tmp_path / "tf"; tf.mkdir()
    (tf / ".env").write_text("export TF_VAR_pve_endpoint='https://localhost:8006/'\n")
    terraform = tmp_path / "bin" / "terraform"
    _stub(terraform, 'echo \'{"vm:200":"pve1"}\'\nexit 42\n')
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, f'echo "$@" >> {tmp_path}/ha-ssh.log\n')

    r = _make(box, "ha", TF_DIR=str(tf), TF=str(terraform), SSH=str(ssh))

    assert r.returncode != 0, f"a failed terraform output was accepted:\n{r.stdout}"
    assert not (tmp_path / "ha-ssh.log").exists(), \
        "the remote HA command ran despite the failed output"


def test_no_per_rule_lookup_is_issued(box, tmp_path):
    """Finding 1. A per-rule `pvesh get` cannot distinguish "no such rule" from
    "the query failed": the upstream handler dies for both and the status is
    not a stable discriminator. The script must not ask that question at all --
    it lists the rules once, checks *that* status, and decides from data.
    """
    proc, _rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve1", resources=[200],
        preset={"prefer-hpe1": "vm:200"}, tag="norule")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = (tmp_path / "ha-stateful-norule" / "calls").read_text().splitlines()
    per_rule = [c for c in calls
                if c.startswith("pvesh get /cluster/ha/rules/")]
    assert not per_rule, f"a per-rule lookup was issued: {per_rule}"
