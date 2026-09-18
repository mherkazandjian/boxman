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
import posixpath
import re
import shutil
import stat
import subprocess
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.unit

BOX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "boxes", "proxmox-nested-ceph-cluster")

PASSWORD = "s3cret-under-test"

#: An ssh stub that actually *executes* the remote command string in a second
#: shell, so word splitting happens the way it does on the far side. `$1` is
#: the host and the rest is the command, exactly as ssh receives them.
SSH_RUNS_REMOTE = """
shift
case "$*" in
  *--list*) printf 'vm:200\\nvm:201\\n'; exit 0 ;;
  *ha-watch.sh*)
      # the one command under test: run it in a real second shell, so the
      # word splitting is the far side's and not Python's idea of it
      bash -c "$*" ;;
  *) exit 0 ;;
esac
"""

#: Stands in for ha-watch.sh and records the argv it was handed.
RECORD_ARGV = """
printf 'ARGC=%s\\n' "$#" >> @LOG@
for a in "$@"; do printf 'ARG=%s\\n' "$a" >> @LOG@; done
"""



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
    tf = tmp_path / "tf"
    tf.mkdir()
    (tf / ".env").write_text("export TF_VAR_pve_api_token='existing'\n")
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, 'echo \'{"value":"leaked-token"}\'\nexit 255\n')

    r = _make(box, "tf-token", SSH=str(ssh), TF_DIR=str(tf))

    assert r.returncode != 0, f"a failed token run reported success:\n{r.stdout}"
    assert (tf / ".env").read_text() == "export TF_VAR_pve_api_token='existing'\n", \
        "existing credentials were replaced from a failed run"


def test_a_run_returning_no_token_is_refused(box, tmp_path):
    tf = tmp_path / "tf"
    tf.mkdir()
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, 'echo "no json here"\n')

    r = _make(box, "tf-token", SSH=str(ssh), TF_DIR=str(tf))

    assert r.returncode != 0
    assert not (tf / ".env").exists()


def test_an_accepted_token_lands_0600(box, tmp_path):
    """The token is root@pam with --privsep 0 (#171 B8)."""
    tf = tmp_path / "tf"
    tf.mkdir()
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
    tf = tmp_path / "tf"
    tf.mkdir()
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
    env = dict(PATH=f"{fake}:{os.environ['PATH']}", BOXMAN_SITE="host2",
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
    r = _make(box, "ha-failover", SSH=str(ssh), ORCH="host1", NODE="pve4")
    assert r.returncode != 0, (
        f"a node that never became ready reported success:\n{r.stdout}\n{r.stderr}")


# ── HA placement policy comes from terraform, not from arithmetic (D1) ───────

_HA_STUB_LIB = r"""
set -euo pipefail
NODES=(pve1 pve2 pve3 pve4)
declare -A NODE_SITE=([pve1]=host1 [pve2]=host1 [pve3]=host2 [pve4]=host2)
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
            if [ -n "${RULES_JSON:-}" ]; then
                echo "$RULES_JSON"
            elif [ "${RULE_EXISTS:-0}" = 1 ]; then
                echo '[{"rule":"prefer-host1","resources":"vm:200"},{"rule":"prefer-host2","resources":"vm:999"}]'
            else
                echo '[]'
            fi
            return 0 ;;
        *"/cluster/options"*)      echo '{"crs":"ha=dynamic"}' ;;
    esac
    return 0
}
"""


def _run_ha(box, tmp_path, homes=None, resources=None, rule_exists=False, tag="x",
            rules_json=None):
    work = tmp_path / f"ha-policy-{tag}"
    work.mkdir(exist_ok=True)
    shutil.copy(box / "scripts" / "pve-ha.sh", work / "pve-ha.sh")
    (work / "lib.sh").write_text(_HA_STUB_LIB)
    res = resources if resources is not None else [200, 201, 202, 203]
    env = dict(os.environ,
               HA_RESOURCES=str([{"sid": f"vm:{i}"} for i in res]).replace("'", '"'),
               RULE_EXISTS="1" if rule_exists else "0")
    if rules_json is not None:
        env["RULES_JSON"] = rules_json
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
    """The discriminating case. `(200 - 200) % 4 == 0` puts vm:200 on host1 by
    the old rule; an override placing it on pve3 must win."""
    proc, calls = _run_ha(
        box, tmp_path, resources=[200],
        homes="vm:200=pve3", tag="override")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rules = [c for c in calls if "pvesh create" in c or "pvesh set /cluster/ha/rules" in c]
    assert any("prefer-host2" in c and "vm:200" in c for c in rules), rules
    assert not any("prefer-host1" in c and "vm:200" in c for c in rules), rules


def test_an_emptied_group_loses_its_rule(box, tmp_path):
    """A group with no members must not keep the membership it had last time."""
    proc, calls = _run_ha(box, tmp_path, resources=[200, 201],
                          homes="vm:200=pve1,vm:201=pve2",
                          rule_exists=True, tag="empty")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("pvesh delete /cluster/ha/rules/prefer-host2" in c for c in calls), calls


def test_a_resource_outside_the_policy_is_left_alone(box, tmp_path):
    """Not filed under whichever group the arithmetic would have picked."""
    proc, calls = _run_ha(box, tmp_path, resources=[200, 999],
                          homes="vm:200=pve1", tag="unowned")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rules = [c for c in calls if "/cluster/ha/rules" in c]
    assert not any("vm:999" in c for c in rules), rules


def test_multiple_victims_cross_the_remote_shell_as_arguments(box, tmp_path):
    """#171 B13, the Makefile half, tested through a real second shell.

    `ha-watch.sh --list` prints one sid per line. Interpolated into the remote
    command with the newlines intact, everything after the first became a
    command of its own.

    The remote command is a *string* a second shell parses, so inspecting it
    with `str.split()` proves nothing: Python treats quote characters as
    ordinary text, so `\' vm:200 vm:201 \'` splits into two plausible tokens
    while the real shell delivers one invalid argument. This runs the string
    through `bash -c` and records the argv the watcher actually receives.
    """
    argv_log = tmp_path / "watch-argv.log"
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, SSH_RUNS_REMOTE)
    recorder = tmp_path / "bin" / "ha-watch.sh"
    _stub(recorder, RECORD_ARGV.replace("@LOG@", str(argv_log)))

    r = _make(box, "ha-failover", SSH=str(ssh), ORCH="host1", NODE="pve4",
              SCRIPTS=str(tmp_path / "bin"))

    assert r.returncode == 0, "the drill failed:\n" + r.stdout + r.stderr
    args = [ln[4:] for ln in argv_log.read_text().splitlines()
            if ln.startswith("ARG=")]
    # The *complete* argv, not its tail: checking only the last two permits
    # dropping the timeout, after which the first sid silently becomes the
    # timeout argument and the drill reports a recovery it never watched.
    assert args == ["pve4", "600", "vm:200", "vm:201"], (
        "the watcher did not receive node, timeout and both sids as four "
        f"separate arguments: {args!r}")



# ── HA rules, against a stub that actually keeps state (D1 follow-ups) ───────

#: Unlike the first stub, this one *retains* rule membership and enforces the
#: feasibility check Proxmox performs: a node-affinity rule is refused if one
#: of its resources already belongs to another. Without that, a rule update
#: that added a member before its old rule released it looked fine.
_HA_STATEFUL_LIB = r"""
set -euo pipefail
NODES=(pve1 pve2 pve3 pve4)
declare -A NODE_SITE=([pve1]=host1 [pve2]=host1 [pve3]=host2 [pve4]=host2)
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
            # exit 0, valid JSON, but the section-config parser warned that it
            # skipped something -- so the listing is not the whole truth
            [ "${RULE_LIST_WARNS:-0}" = 1 ] && {
                echo "ignoring invalid configuration line" >&2
                echo '[{"rule":"prefer-host1","resources":"vm:200"}]'; return 0; }
            [ "${RULE_LIST_SHAPE:-}" = notarray ] && { echo '{"oops":true}'; return 0; }
            [ "${RULE_LIST_SHAPE:-}" = noname ] && {
                echo '[{"resources":"vm:200"}]'; return 0; }
            # valid names, so the shape checks pass, but @tsv cannot render the
            # second entry: jq emits one row and *then* fails
            [ "${RULE_LIST_SHAPE:-}" = tsvfail ] && {
                echo '[{"rule":"prefer-host1","resources":"vm:200"},{"rule":"prefer-host2","resources":{}}]'
                return 0; }
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
        preset={"prefer-host1": "vm:200", "prefer-host2": "vm:201"}, tag="move")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "vm:201" in rules.get("prefer-host1", ""), rules
    assert "vm:201" not in rules.get("prefer-host2", ""), rules


def test_a_simultaneous_swap_is_applied(box, tmp_path):
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve3,vm:201=pve1", resources=[200, 201],
        preset={"prefer-host1": "vm:200", "prefer-host2": "vm:201"}, tag="swap")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rules.get("prefer-host1", "") == "vm:201", rules
    assert rules.get("prefer-host2", "") == "vm:200", rules


def test_a_failed_inventory_does_not_delete_a_rule(box, tmp_path):
    """`mapfile < <(cmd)` hides the failure even under pipefail, so a partial
    list was used as if complete and the cleanup deleted a live rule."""
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve1,vm:201=pve3", resources=[200],
        preset={"prefer-host2": "vm:201"}, tag="inv", INVENTORY_FAILS=1)

    assert proc.returncode != 0, proc.stdout
    assert "prefer-host2" in rules, "a rule was deleted on the strength of a failed query"
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
        preset={"prefer-host2": "vm:201"}, tag="list", RULE_LIST_FAILS=1)

    assert proc.returncode != 0, (
        f"a failed rule listing was read as 'no rules':\n{proc.stdout}")
    assert "prefer-host2" in rules, "a stale rule was deleted on a failed listing"
    calls = (tmp_path / "ha-stateful-list" / "calls").read_text()
    assert "pvesh set /cluster/options" not in calls, \
        "CRS was changed despite the failed listing"


def test_make_ha_refuses_a_failed_terraform_output(box, tmp_path):
    """Terraform emitting valid JSON and then failing still triggered the
    remote HA command; make exited 0 (#171 D1 follow-up)."""
    tf = tmp_path / "tf"
    tf.mkdir()
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
        preset={"prefer-host1": "vm:200"}, tag="norule")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = (tmp_path / "ha-stateful-norule" / "calls").read_text().splitlines()
    per_rule = [c for c in calls
                if c.startswith("pvesh get /cluster/ha/rules/")]
    assert not per_rule, f"a per-rule lookup was issued: {per_rule}"


@pytest.mark.parametrize("env,why", [
    ({"RULE_LIST_WARNS": 1}, "the rule config did not parse cleanly"),
    ({"RULE_LIST_SHAPE": "notarray"}, "the listing is not an array"),
    ({"RULE_LIST_SHAPE": "noname"}, "an entry has no rule name"),
    # the converter emits a usable prefix and then fails -- the exact shape of
    # the process-substitution defect this replaced
    ({"RULE_LIST_SHAPE": "tsvfail"}, "the tsv conversion failed after a row"),
])
def test_an_untrustworthy_rule_listing_prevents_every_write(box, tmp_path, env, why):
    """Finding 1. Exit 0 with valid JSON is not a certificate: Proxmox's
    section-config parser warns on stderr and *skips* a malformed header or an
    unknown section type, so a rule can vanish from the listing with nothing in
    the status to say so. And parsing the listing through
    `< <(jq … || true)` let a jq that emitted some entries and then failed
    produce a partial inventory -- the same process-substitution defect as B11
    and the HA inventory read, for the third time.
    """
    tag = "untrust-" + (list(env.values())[0] if isinstance(list(env.values())[0], str) else "warn")
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve1", resources=[200],
        preset={"prefer-host2": "vm:201"}, tag=tag, **env)

    assert proc.returncode != 0, f"{why}, but the run continued:\n{proc.stdout}"
    calls = (tmp_path / ("ha-stateful-" + tag) / "calls").read_text()
    assert "pvesh set" not in calls and "pvesh create" not in calls \
        and "pvesh delete" not in calls, f"a write happened although {why}"
    assert "prefer-host2" in rules, "a rule was removed on an untrustworthy listing"


def test_an_empty_rule_listing_is_a_valid_answer(box, tmp_path):
    """A cluster with no rules yet must not be mistaken for a failure -- the
    listing callback returns exit 0 and `[]` for a clean empty configuration."""
    proc, rules = _run_ha_stateful(
        box, tmp_path, homes="vm:200=pve1", resources=[200], preset={}, tag="emptylist")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rules.get("prefer-host1", "") == "vm:200", rules


# ── the fourth process substitution, in the migration script ────────────────

#: `ping` must stay alive until the script signals it: a stub that returns at
#: once leaves `kill -INT $pingpid` failing, which ends the run before the
#: placement check and made a "successful migration" control pass on a script
#: that exited 1. `command sleep` because lib.sh's sleep is stubbed out, and
#: only two seconds because bash cannot act on the INT until the command it is
#: waiting on returns -- so `wait` blocks for however long this sleeps.
#:
#: Placement is state, not a constant: the resource query answers pve1 until a
#: migration succeeds and pve3 afterwards, so the script's final placement
#: assertion has something true to find.
_MIGRATE_STUB_LIB = r"""
set -euo pipefail
NODES=(pve1 pve2 pve3 pve4)
declare -A NODE_IP=([pve1]=10.77.0.11 [pve2]=10.77.0.12 [pve3]=10.77.0.13 [pve4]=10.77.0.14)
declare -A NODE_SITE=([pve1]=host1 [pve2]=host1 [pve3]=host2 [pve4]=host2)
LOGS="$PWD"; CALLS="$PWD/calls"; : > "$CALLS"
PLACE="$PWD/placement"; echo pve1 > "$PLACE"
SSH_OPTS=(-o BatchMode=yes)
DEMO_VMID=100; DEMO_NAME=demo01; DEMO_IP=10.77.0.50
log() { :; }
die() { echo "die $*" >> "$CALLS"; exit 1; }
sleep() { :; }
ping() { case "$*" in *-w*) command sleep 2 ;; *) return 0 ;; esac; }
ssh()  { return 0; }
pssh() {
    local node=$1; shift; local cmd="$*"
    echo "$cmd" >> "$CALLS"
    case "$cmd" in
        *"/cluster/resources"*)
            printf '[{"vmid":100,"node":"%s","name":"demo01","status":"running"}]' "$(cat "$PLACE")"
            [ "${LOOKUP_FAILS:-0}" = 1 ] && return 255
            return 0 ;;
        *"qm migrate"*)
            echo "migrate $cmd" >> "$CALLS"
            [ "${MIGRATE_FAILS:-0}" = 1 ] && return 1
            echo pve3 > "$PLACE"; return 0 ;;
    esac
    return 0
}
"""

#: A jq that emits the valid source row and *then* fails -- the producer-side
#: half of the defect. The SSH fixture stops before jq is reached, so without
#: this the new parser-status check is never exercised.
_JQ_ROW_THEN_FAIL = """
printf 'pve1 demo01\n'
exit 5
"""


def _run_migrate(box, tmp_path, tag, jq_stub=None, **env):
    work = tmp_path / ("migrate-" + tag)
    work.mkdir()
    shutil.copy(box / "scripts" / "pve-migrate.sh", work / "pve-migrate.sh")
    (work / "lib.sh").write_text(_MIGRATE_STUB_LIB)
    path = os.environ["PATH"]
    if jq_stub is not None:
        binn = work / "bin"
        binn.mkdir()
        _stub(binn / "jq", jq_stub)
        path = f"{binn}:{path}"
    proc = subprocess.run(
        ["bash", "./pve-migrate.sh", "pve3"], cwd=work, timeout=120,
        env=dict(os.environ, PATH=path, **{k: str(v) for k, v in env.items()}),
        capture_output=True, text=True)
    calls = (work / "calls")
    return proc, (calls.read_text() if calls.exists() else "")


def test_a_migration_never_starts_from_a_failed_source_lookup(box, tmp_path):
    """`read -r src … < <(pssh … | jq …)` sees whether *read* got a row, not
    whether the producer finished, so ssh printing the valid row and then
    exiting 255 left a usable-looking source node and `qm migrate` ran anyway.
    Predates this work (caa4be2); found by the wider scan for the pattern."""
    proc, calls = _run_migrate(box, tmp_path, "sshfail", LOOKUP_FAILS=1)

    assert proc.returncode != 0, "a failed lookup still reported success:\n" + proc.stdout
    assert "migrate" not in calls, "qm migrate ran after the source lookup failed"


def test_a_migration_never_starts_from_a_failed_parse(box, tmp_path):
    """The other half: the lookup succeeds and the *parser* fails after
    emitting a usable row. The ssh fixture stops before jq is reached, so it
    cannot exercise this."""
    proc, calls = _run_migrate(box, tmp_path, "jqfail", jq_stub=_JQ_ROW_THEN_FAIL)

    assert proc.returncode != 0, "a failed parse still reported success:\n" + proc.stdout
    assert "migrate" not in calls, "qm migrate ran after the parse failed"


def test_a_successful_migration_runs_to_completion(box, tmp_path):
    """The positive control. Without it the checks above could pass by
    refusing every migration -- and a control that only looks for the command
    having *started* is not one: the earlier version passed on a script that
    exited 1, because the ping stub had already gone and the kill failed."""
    proc, calls = _run_migrate(box, tmp_path, "ok")

    assert proc.returncode == 0, (
        "the unmodified script did not complete:\n" + proc.stdout + proc.stderr)
    assert "qm migrate 100 pve3" in calls, calls
    # the placement assertion the script makes must have had something to find
    assert calls.count("/cluster/resources") >= 2, (
        "the final placement was never queried: " + calls)


# --- site propagation -------------------------------------------------------
#
# `lib.sh` resolves SITE from BOXMAN_SITE and falls back to `hostname -s`. The
# fallback is a trap rather than a convenience: on the machines this box was
# written against, `hostname -s` returned the *old* site names, so every call
# that forgot BOXMAN_SITE worked by coincidence until the rename -- and then
# aborted with "unknown site". `make all` does not exercise tf-token, ha,
# ha-status or ha-failover, so two clean end-to-end runs said nothing about
# them; a reviewer found four broken targets afterwards.
#
# These are static assertions on the Makefile on purpose. The failure mode is
# "one call site out of N was missed", and only reading all N catches it.

def _makefile_lines() -> list[tuple[int, str]]:
    with open(os.path.join(BOX, "Makefile")) as fh:
        return list(enumerate(fh.read().splitlines(), 1))


def test_every_orchestration_call_forwards_the_site():
    """Any `$(SSH) $(ORCH) …` that reaches lib.sh must set BOXMAN_SITE."""
    offenders = [
        (n, line.strip()) for n, line in _makefile_lines()
        if "$(SSH) $(ORCH)" in line and "BOXMAN_SITE=$(ORCH)" not in line
    ]
    assert not offenders, (
        "orchestration-host calls not forwarding BOXMAN_SITE=$(ORCH) (lib.sh "
        f"would fall back to `hostname -s`, or to the wrong site): {offenders}")


def test_the_site_macros_forward_the_site():
    """The two macros every other recipe is supposed to go through."""
    text = "\n".join(line for _, line in _makefile_lines())
    for macro in ("define run", "define boxman"):
        body = text.split(macro, 1)[1].split("endef", 1)[0]
        assert "BOXMAN_SITE=$(1)" in body, f"{macro} does not forward the site"




# ── executed counterparts to the static site checks ─────────────────────────
#
# The static assertions above read the Makefile. That catches a missed call
# site, but a reviewer showed they all pass when the calls forward a *wrong*
# site, and when sync's setup check is deleted. These run the targets and look
# at what the far side actually received.

#: A remote `lib.sh` that enforces the real site guard and records what it
#: resolved. An unknown site must FAIL here exactly as lib.sh does on a host:
#: a stub that only records the command *text* cannot tell a correct
#: BOXMAN_SITE from a second, wrong one appended after it -- which is how the
#: first version of these tests stayed green under that mutation.
_SITE_PROBE_LIB = """
SITE=${{BOXMAN_SITE:-$(hostname -s)}}
declare -A HOST_IP=([host1]=10.0.0.1 [host2]=10.0.0.2)
declare -A NODE_SITE=([pve1]=host1 [pve2]=host1 [pve3]=host2 [pve4]=host2)
declare -A PEER=([host1]=host2 [host2]=host1)
NODES=(pve1 pve2 pve3 pve4)
if [[ -z ${{HOST_IP[$SITE]:-}} ]]; then
    echo "ERROR: unknown site '$SITE'" >&2
    exit 1
fi
printf '%s\\n' "$SITE" >> {log}
# tf-token extracts the secret from pssh's output and refuses an empty one
pssh() {{ case "$*" in *"token add"*) echo '{{"value":"stub-token"}}' ;; esac; }}
log() {{ :; }}
die() {{ echo "$*" >&2; exit 1; }}
"""


def _site_probe(tmp_path):
    """Everything the four targets touch on the far side, all of it succeeding.

    Returns the remote scripts dir, the log of the site each remote call ended
    up with, an ssh that *executes* its command, and a boxman stand-in. The
    targets have to be able to run to a clean exit: one that stops early -- at
    a missing stub, or at a wrong site on a later call -- leaves its earlier,
    correct calls in the log, and a test that ignores the exit status then
    passes on those.
    """
    scripts = tmp_path / "remote-scripts"
    scripts.mkdir(exist_ok=True)
    log = tmp_path / "resolved-sites"
    (scripts / "lib.sh").write_text(_SITE_PROBE_LIB.format(log=log))
    for name in ("pve-ha.sh", "ha-watch.sh", "wait-first-boot.sh"):
        _stub(scripts / name, 'source "$(dirname "$0")/lib.sh"\nexit 0\n')
    binn = tmp_path / "bin"
    ssh = binn / "ssh-stub"
    _stub(ssh, 'shift\nbash -c "$*"\n')      # run it, do not just record it
    # ha-failover kills the node with `virsh destroy` over ssh, which the
    # executing stub above would otherwise run against THIS machine's libvirt.
    # bin/ leads PATH for everything make runs.
    _stub(binn / "virsh", "exit 0\n")
    # lib.sh falls back to `hostname -s`; pin it so a call that relies on the
    # fallback is refused on every machine, not only on ones not named host1
    _stub(binn / "hostname", "echo not-a-site\n")
    # `boxman up` on the victim's host does not go through lib.sh; its stand-in
    # records the site it was handed in the same log
    boxman = binn / "boxman-stub"
    _stub(boxman, f'printf \'%s\\n\' "${{BOXMAN_SITE:-UNSET}}" >> {log}\n')
    return scripts, log, ssh, boxman


#: The site each remote call must end up with, per target, in call order.
#: Everything goes to the orchestrator except `boxman up`, which restores the
#: killed node on the host that owns it.
@pytest.mark.parametrize("target,extra,expected", [
    ("tf-token", {}, ["host1"]),
    ("ha-status", {}, ["host1"]),
    ("ha", {}, ["host1"]),
    ("ha-failover", {"NODE": "pve4"}, [
        "host1",    # which host owns the node
        "host1",    # ha-watch.sh --list: the victims, taken before the kill
        "host1",    # ha-watch.sh <node> 600 <victims>
        "host2",    # boxman up, on the node's own host
        "host1",    # wait-first-boot.sh <node>
    ]),
])
def test_orchestration_targets_deliver_the_orchestrator_site(box, tmp_path, target, extra,
                                                              expected):
    """The site each remote call *resolves* must be the one the target meant.

    `lib.sh` falls back to `hostname -s`, which on the machines this box grew up
    on returned the old site names -- so a call that forgets the variable works
    by coincidence until something is renamed, then aborts with "unknown site".

    Make has to succeed, and the log has to hold one entry per remote call in
    order. The previous version collapsed the log into a set and ignored the
    exit status, so ha-failover stopping at an unstubbed virsh -- or at a wrong
    site on its watcher call -- still left {"host1"} from the calls before it.
    """
    scripts, log, ssh, boxman = _site_probe(tmp_path)
    tf = tmp_path / "tf"
    tf.mkdir(exist_ok=True)
    (tf / ".env").write_text("export TF_VAR_pve_api_token='x'\n")
    terraform = tmp_path / "bin" / "terraform"
    _stub(terraform, 'echo \'{"vm:200":"pve1"}\'\n')

    r = _make(box, target, SSH=str(ssh), ORCH="host1", SCRIPTS=str(scripts),
              TF_DIR=str(tf), TF=str(terraform), REMOTE_BOX=str(tmp_path),
              BOXMAN=str(boxman), **extra)

    assert r.returncode == 0, (
        f"{target} did not run to completion:\n{r.stdout}{r.stderr}")
    resolved = log.read_text().split() if log.exists() else []
    assert resolved == expected, (
        f"{target}: its remote calls ended up with the sites {resolved}, in "
        f"order; expected {expected}")


def test_sync_does_not_retarget_after_a_failed_setup_on_the_second_host(box, tmp_path):
    """host2's transfers must not be re-aimed at host1.

    The alias used to be resolved after an `&&` and followed by `;`: a failed
    mkdir on the second host skipped the assignment but not the rsyncs, so `a`
    still held the FIRST host's alias, both transfers went there again, and
    sync exited 0. Deleting the setup check reproduces it, which is why this
    is executed rather than read off the Makefile.
    """
    log = tmp_path / "rsync-targets"
    _stub(tmp_path / "bin" / "rsync", f'printf "%s\\n" "${{@: -1}}" >> {log}\n')
    ssh = tmp_path / "bin" / "ssh-stub"
    _stub(ssh, 'if [ "$1" = host2 ]; then exit 1; fi\nexit 0\n')

    r = _make(box, "sync", SSH=str(ssh),
              env={"SSH_ALIAS_host1": "alpha", "SSH_ALIAS_host2": "beta"})

    assert r.returncode != 0, "sync reported success after host2's setup failed"
    targets = log.read_text().split() if log.exists() else []
    assert not [t for t in targets if t.startswith("beta:")], \
        f"transfers reached host2 despite its setup failing: {targets}"
    assert len([t for t in targets if t.startswith("alpha:")]) == 2, \
        f"host2's transfers were re-aimed at host1: {targets}"


def test_rules_persisted_under_the_old_site_names_are_migrated(box, tmp_path):
    """A lab built before the host1/host2 rename holds prefer-hpe1/2.

    Proxmox refuses a resource that already belongs to another rule, so unless
    the legacy rule is released first, creating prefer-host1 fails for good --
    and the withdrawal loop only knows the new names.

    Asserting the *end state* rather than the call sequence: an earlier version
    of this test allowed `created is None`, so deleting both legacy rules and
    exiting without creating anything passed it, leaving the cluster with no
    affinity rules at all.
    """
    proc, rules = _run_ha_stateful(
        box, tmp_path,
        homes="vm:200=pve1,vm:201=pve1,vm:202=pve3,vm:203=pve3",
        resources=[200, 201, 202, 203],
        preset={"prefer-hpe1": "vm:200,vm:201", "prefer-hpe2": "vm:202,vm:203"},
        tag="legacy")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "prefer-hpe1" not in rules, f"legacy rule survived: {rules}"
    assert "prefer-hpe2" not in rules, f"legacy rule survived: {rules}"
    assert rules.get("prefer-host1", "") == "vm:200,vm:201", rules
    assert rules.get("prefer-host2", "") == "vm:202,vm:203", rules


def test_a_legacy_member_moving_to_the_other_host_is_migrated(box, tmp_path):
    """Migration and a cross-host move in the same run: vm:201 was on the old
    host1 rule and now belongs to host2."""
    proc, rules = _run_ha_stateful(
        box, tmp_path,
        homes="vm:200=pve1,vm:201=pve3",
        resources=[200, 201],
        preset={"prefer-hpe1": "vm:200,vm:201"},
        tag="legacymove")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "prefer-hpe1" not in rules, rules
    assert rules.get("prefer-host1", "") == "vm:200", rules
    assert rules.get("prefer-host2", "") == "vm:201", rules


def test_no_legacy_delete_on_a_cluster_that_never_had_them(box, tmp_path):
    """The migration must be a no-op where it does not apply."""
    proc, calls = _run_ha(box, tmp_path, homes="vm:200=pve1", rules_json="[]",
                          tag="nolegacy")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not [c for c in calls if "prefer-hpe" in c], \
        f"touched rules that do not exist: {calls}"





# ── first boot: the upgrade that keeps PVE and Ceph in step (#171, 2026-09-15) ─
#
# The nodes install from a frozen ISO while `pveceph install` takes whatever
# Ceph is current, so an un-upgraded node runs ISO-era PVE against a newer
# Ceph. On 2026-09-15 that pairing rejected the rbd keyring PVE had just
# written and left every storage inactive on a HEALTH_OK cluster. The fix is a
# `dist-upgrade` in the first-boot hook.
#
# The marker is what the whole lab waits on: wait-first-boot.sh polls it and
# treats it as certifying repositories, MTU and the guest agent, so it must
# appear only after every prerequisite has succeeded. *Every* stub the hook
# calls -- apt, systemctl, ifreload, ip -- therefore reports whether the marker
# already exists when it runs, and the tests demand that none of them saw it.
#
# The hook is run from a copy whose absolute paths are rebased under a temp
# root. That is a rewrite for *reachability*, not a sandbox: it proves control
# flow and ordering, and a hook that grew a new absolute path would write to
# the real one. Containment is the disposable VM's job, not this fixture's.

#: Reports the marker if it is already there, then does whatever the test needs.
#: Every prerequisite the hook shells out to wears this.
MARKER_WATCH = """
printf '@NAME@ %s\\n' "$*" >> @LOG@
[ -e @MARKER@ ] && printf 'MARKER-PRESENT-AT @NAME@\\n' >> @LOG@
@BODY@
exit 0
"""

#: apt-get stub. Records each invocation, parses the options apt would forward
#: to dpkg, snapshots the sources as apt saw them, and acts on the mode.
#:
#: Parsing rather than substring-matching matters: counting arguments that
#: merely contain `--force-confold` accepts `-o Dpkg::Option::=--force-confold`,
#: a misspelt key apt puts in no list at all. Keys fold case, because apt's
#: configuration keys are case-insensitive and `DPkg::Options::` is apt's own
#: spelling.
APT_STUB = """
printf 'apt %s\\n' "$*" >> @LOG@
mode=; prev=; dpkg_opts=
for a in "$@"; do
    case "$a" in
        update|dist-upgrade|install) [ -n "$mode" ] || mode=$a ;;
    esac
    if [ "$prev" = "-o" ]; then
        key=${a%%=*}; val=${a#*=}
        lkey=$(printf '%s' "$key" | tr 'A-Z' 'a-z')
        [ "$lkey" = "dpkg::options::" ] && dpkg_opts="$dpkg_opts $val"
    fi
    prev=$a
done

[ -e @MARKER@ ] && printf 'MARKER-PRESENT-AT %s\\n' "${mode:-apt}" >> @LOG@

if [ "$mode" = update ]; then
    # A fresh directory per update, because one shared snapshot records only
    # the last state apt saw: an earlier update against a wrong configuration
    # -- enterprise still on, no no-subscription source yet -- would be
    # overwritten by the correct later one and never noticed.
    #
    # -L, because `cp -a` would preserve a symlink and the bytes behind it
    # could change before the assertions read them. What is stored has to be
    # what apt read at the moment it ran.
    n=$(( $(cat @SNAPSHOT@/.count 2>/dev/null || echo 0) + 1 ))
    printf '%s' "$n" > @SNAPSHOT@/.count
    mkdir -p @SNAPSHOT@/update-$n/parts
    cp -rL @SOURCES@/. @SNAPSHOT@/update-$n/parts/ 2>/dev/null
    [ -f @MAINLIST@ ] && cp -L @MAINLIST@ @SNAPSHOT@/update-$n/sources.list
    printf '%s\\n' "$*" > @SNAPSHOT@/update-$n/argv
    # NUL-delimited, because `$*` cannot say whether `update -qq` was one
    # argument or two, nor whether an empty one was passed -- and APT rejects
    # both of those spellings outright
    printf '%s\\0' "$@" > @SNAPSHOT@/update-$n/argv0
    printf '%s' "${APT_CONFIG-}" > @SNAPSHOT@/update-$n/apt_config
    # APT reads apt.conf and apt.conf.d/* on every run, so a hook can change
    # what gets selected and delete the evidence afterwards. Recorded here,
    # while it is still true.
    mkdir -p @SNAPSHOT@/update-$n/aptconf
    present=@SNAPSHOT@/update-$n/aptconf-present
    # presence is recorded by existence, not by managing to read it: a config
    # this stub cannot copy is still a config APT loads -- it runs as root,
    # where mode 000 is no obstacle -- and a silently empty capture would say
    # the opposite
    if [ -e @APTETC@/apt.conf ]; then
        printf 'apt.conf\\n' >> "$present"
        cp -L @APTETC@/apt.conf @SNAPSHOT@/update-$n/aptconf/apt.conf \
            || printf 'apt.conf (unreadable)\\n' >> "$present"
    fi
    if [ -e @APTETC@/apt.conf.d ]; then
        ls -A @APTETC@/apt.conf.d >> "$present" 2>/dev/null \
            || printf 'apt.conf.d (unreadable)\\n' >> "$present"
        cp -rL @APTETC@/apt.conf.d @SNAPSHOT@/update-$n/aptconf/ 2>/dev/null \
            || printf 'apt.conf.d (uncopyable)\\n' >> "$present"
    fi
    for f in @SOURCES@/*; do
        [ -L "$f" ] && printf 'SYMLINKED-SOURCE %s\\n' "$f" >> @LOG@
    done
    printf 'SOURCES-SNAPSHOT %s\\n' "$n" >> @LOG@
fi

if [ "$mode" = dist-upgrade ]; then
@UPGRADE@
fi
if [ "$mode" = install ]; then
@INSTALL@
fi
exit 0
"""

#: A dist-upgrade meeting a conffile edited locally *and* changed in the new
#: package -- the conflict step 1 of the hook creates by editing
#: pve-enterprise.sources. dpkg asks; with null stdin the answer is EOF and apt
#: dies, and DEBIAN_FRONTEND does not help because dpkg reads that prompt
#: itself.
#:
#: Any one of the three force options answers *this* conflict: dpkg's own
#: default for it is keep-old, so `--force-confdef` returns that without
#: reading stdin, and confold/confnew answer it outright. That the hook sends a
#: pair is policy, asserted separately; a behavioural test cannot see one of
#: them go missing, and modelling confdef as insufficient would fail a
#: legitimate hook.
CONFFILE_PROMPT = """
    answered=0
    for o in $dpkg_opts; do
        case "$o" in
            --force-confold|--force-confnew|--force-confdef) answered=1 ;;
        esac
    done
    if [ "$answered" -eq 0 ]; then
        echo "Configuration file '/etc/apt/sources.list.d/pve-enterprise.sources'" >&2
        echo "EOF on stdin at conffile prompt" >&2
        exit 1
    fi
"""

#: A failing step that says something recognisable on stderr. Without a
#: diagnostic, a log test can pass on the startup banner alone while the hook's
#: `exec` has stopped capturing stderr at all -- the dead end the log exists to
#: prevent.
STEP_FAILS = """
    echo "PVE_LAB_TEST_APT_BROKE: held broken packages" >&2
    exit 100
"""


def _first_boot(box, tmp_path, upgrade: str, *, install: str = "    :",
                systemctl: str = ":", ifreload: str = ":", ip: str = ":"):
    """
    Run the real hook with its absolute paths rebased under a temp root.

    Returns ``(completed_process, marker, event_log, hook_log, sources_snapshot)``.
    """
    root = tmp_path / "root"
    for d in ("var/lib", "var/log", "etc/apt/sources.list.d", "etc/network"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "etc/network/interfaces").write_text(
        "auto ens18\niface ens18 inet manual\n\n"
        "auto vmbr0\niface vmbr0 inet static\n        bridge-ports ens18\n")
    # what the ISO actually ships, so "the enterprise repo is still on" is a
    # state the assertions can see rather than an empty stanza that matches
    # nothing whatever the hook does to it
    (root / "etc/apt/sources.list.d/pve-enterprise.sources").write_text(
        "Types: deb\n"
        "URIs: https://enterprise.proxmox.com/debian/pve\n"
        "Suites: trixie\n"
        "Components: pve-enterprise\n"
        "Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n"
        "Enabled: true\n")
    # the hook's disabling loop names this one too, and it is a different
    # repository with a different component -- seeded so that dropping it from
    # that loop is something the assertions can see
    (root / "etc/apt/sources.list.d/ceph.sources").write_text(
        "Types: deb\n"
        "URIs: https://enterprise.proxmox.com/debian/ceph-squid\n"
        "Suites: trixie\n"
        "Components: enterprise\n"
        "Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n"
        "Enabled: true\n")

    src = (box / "scripts" / "first-boot.sh").read_text()
    for absolute in ("/var/lib/pve-lab", "/var/log/pve-lab-first-boot.log",
                     "/etc/apt/sources.list.d", "/etc/network/interfaces"):
        assert absolute in src, f"the hook no longer writes {absolute}"
    # /etc/apt wholesale, not just sources.list.d: APT's default configuration
    # lives beside it, and a hook that wrote apt.conf.d would otherwise reach
    # the real one -- changing what APT selects without touching any input this
    # fixture records
    for absolute in ("/var/lib/pve-lab", "/var/log/pve-lab-first-boot.log",
                     "/etc/apt", "/etc/network/interfaces"):
        src = src.replace(absolute, f"{root}{absolute}")
    hook = tmp_path / "first-boot.sh"
    hook.write_text(src)

    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    log = tmp_path / "events.log"
    marker = root / "var/lib/pve-lab/first-boot.done"
    snapshot = tmp_path / "sources-as-apt-saw-them"
    snapshot.mkdir(exist_ok=True)

    def _fill(body, **extra):
        body = (body.replace("@LOG@", str(log))
                    .replace("@MARKER@", str(marker))
                    .replace("@SOURCES@", str(root / "etc/apt/sources.list.d"))
                    .replace("@SNAPSHOT@", str(snapshot))
                    .replace("@MAINLIST@", str(root / "etc/apt/sources.list"))
                    .replace("@APTETC@", str(root / "etc/apt")))
        for key, value in extra.items():
            body = body.replace(key, value)
        return body

    _stub(fake / "apt-get",
          _fill(APT_STUB, **{"@UPGRADE@": upgrade, "@INSTALL@": install}))
    for name, body in (("systemctl", systemctl), ("ifreload", ifreload),
                       ("ip", ip)):
        _stub(fake / name,
              _fill(MARKER_WATCH, **{"@NAME@": name, "@BODY@": body}))

    hook_env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}")
    # cleared, so that an APT_CONFIG recorded below is one the hook set itself
    hook_env.pop("APT_CONFIG", None)
    r = subprocess.run(
        ["bash", str(hook)], capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL,       # as the service runs it: no answers
        env=hook_env)
    return (r, marker, log.read_text() if log.exists() else "",
            root / "var/log/pve-lab-first-boot.log", snapshot)


def _dpkg_options_of(events: str) -> list[str]:
    """The values the hook forwarded under apt's `DPkg::Options::` key."""
    line = next(ln for ln in events.splitlines() if "dist-upgrade" in ln)
    args = line.split()
    return [a.split("=", 1)[1]
            for prev, a in zip(args, args[1:], strict=False)   # pairwise
            if prev == "-o" and a.split("=", 1)[0].lower() == "dpkg::options::"]


KNOWN_TYPES = {"deb", "deb-src"}
#: values APT reads as false. Anything else -- including "off extra" -- leaves
#: the entry enabled, so the whole field is compared, not its first word.
FALSE_VALUES = {"no", "false", "0", "off"}
DEFAULT_PORTS = {"http": 80, "https": 443}


def _deb822_stanzas(text: str) -> list[dict[str, list[str]]]:
    """
    Parse deb822 into stanzas of ``{field: [values]}``.

    A grep cannot do this job: sources.list(5) allows any spacing after the
    colon, values continued on indented lines, and whitespace-separated
    multivalues -- and a field matched anywhere in a file says nothing about
    which *stanza* it belongs to, which is what selects a repository.
    """
    stanzas: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    last: str | None = None
    for raw in text.replace("\r\n", "\n").splitlines():
        if not raw.strip():
            if current:
                stanzas.append(current)
            current, last = {}, None
            continue
        if raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t":                       # continuation of the field
            if last:
                current[last].extend(raw.split())
            continue
        if ":" not in raw:
            current["__malformed__"] = ["1"]
            continue
        field, _, value = raw.partition(":")
        last = field.strip().lower()
        current[last] = value.split()
    if current:
        stanzas.append(current)
    return stanzas


def _one_line_entries(text: str) -> list[dict[str, list[str]]]:
    """
    The `.list` one-line format, in the same shape.

    APT reads both formats from the same directory, so a check that looks only
    at `.sources` can miss an enterprise entry -- or refuse a perfectly good
    no-subscription one -- purely because of the filename. Options in brackets
    are kept rather than discarded: a `[signed-by=...]` that APT rejects makes
    the whole configuration unusable, so it has to reach the validation below.
    """
    entries = []
    for raw in text.replace("\r\n", "\n").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split(" #", 1)[0]                   # trailing comment
        options = dict(
            opt.split("=", 1)
            for block in re.findall(r"\[([^\]]*)\]", line)
            for opt in block.split() if "=" in opt)
        parts = re.sub(r"\[[^\]]*\]", " ", line).split()
        if len(parts) < 3:
            entries.append({"__malformed__": ["1"]})
            continue
        # the type is kept even when unknown: APT refuses the file rather than
        # skipping the line, and the validator has to see that
        entry = {"types": [parts[0]], "uris": [parts[1]],
                 "suites": [parts[2]], "components": parts[3:]}
        if "signed-by" in options:
            entry["signed-by"] = options["signed-by"].split(",")
        # `[arch=...]` is the one-line spelling of Architectures, and dropping
        # it made an arm64-only entry look native
        if "arch" in options:
            entry["architectures"] = options["arch"].split(",")
        if "arch-" in options:
            entry["architectures-remove"] = options["arch-"].split(",")
        entries.append(entry)
    return entries


#: APT ignores a file in sources.list.d whose name is not made of these
#: characters, so `proxmox saved.sources` configures nothing at all.
VALID_SOURCE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _apt_stanzas(update_dir) -> list[dict[str, list[str]]]:
    """
    Every entry APT would read for one intercepted `apt-get update`.

    That is the main `sources.list` as well as the `sources.list.d` parts:
    an enterprise entry added to the main list is not less active for being
    outside the directory this fixture used to look at.
    """
    stanzas: list[dict[str, list[str]]] = []
    main = update_dir / "sources.list"
    if main.exists():
        stanzas.extend(_one_line_entries(main.read_text()))
    parts = update_dir / "parts"
    if parts.is_dir():
        for path in sorted(parts.iterdir()):
            if not VALID_SOURCE_NAME.match(path.name):
                continue
            if path.suffix == ".sources":
                stanzas.extend(_deb822_stanzas(path.read_text()))
            elif path.suffix == ".list":
                stanzas.extend(_one_line_entries(path.read_text()))
    return stanzas


def _is_enabled(stanza: dict[str, list[str]]) -> bool:
    """`Enabled:` is per stanza, and only its exact false spellings count."""
    values = stanza.get("enabled")
    if not values:
        return True
    return " ".join(values).strip().lower() not in FALSE_VALUES


def _configuration_loads(stanzas: list[dict[str, list[str]]]) -> bool:
    """
    Whether APT could load this configuration at all.

    An unknown type, a missing required field, a malformed line or a
    `Signed-By` APT rejects is not an entry it skips: it refuses the file, and
    nothing in it is used -- including the entry the hook wanted. A selector
    that only looks for the tokens it wants calls such a configuration good.

    This models the failures reachable from what this hook writes. It is not a
    general APT validator; see the parser controls for the cases checked
    against real APT.
    """
    for stanza in stanzas:
        if "__malformed__" in stanza:
            return False
        types = stanza.get("types", [])
        if not types or any(t not in KNOWN_TYPES for t in types):
            return False
        uris = stanza.get("uris")
        if not uris or not stanza.get("suites"):
            return False
        # An inline comment is not a comment in a deb822 field: it becomes more
        # field content, and APT refuses the entry. Finding one good URI among
        # the tokens is not the same as the stanza being loadable.
        if any("://" not in uri for uri in uris):
            return False
        # an exact-path suite takes no components, and a named suite requires
        # them; APT refuses the file either way round, including when one bad
        # exact-path member sits beside a valid suite
        exact = [su for su in stanza["suites"] if su.endswith("/")]
        named = [su for su in stanza["suites"] if not su.endswith("/")]
        if exact and stanza.get("components"):
            return False
        if named and not stanza.get("components"):
            return False
        signed_by = stanza.get("signed-by")
        if signed_by:
            tokens = [t for t in re.split(r"[,\s]+", " ".join(signed_by)) if t]
            paths = tokens and all(t.startswith("/") for t in tokens)
            # exactly 40 hex digits, optionally suffixed '!' -- 41 is not a
            # fingerprint and APT says so
            fingerprint = (len(tokens) == 1
                           and re.fullmatch(r"[0-9A-Fa-f]{40}!?", tokens[0]))
            if not (paths or fingerprint):
                return False
    return True


def _points_at(stanza: dict[str, list[str]], host: str, path: str) -> bool:
    """
    Whether any of the stanza's URIs is *host*/*path* over HTTP(S).

    Compared on structure: the hostname folds case and the default port is the
    same endpoint, while `file://` with the same spelling is a different
    resource entirely.
    """
    for uri in stanza.get("uris", []):
        parts = urlsplit(uri)
        if parts.scheme not in DEFAULT_PORTS:
            continue
        if _canonical_host(parts.hostname) != host:
            continue
        if parts.port is not None and parts.port != DEFAULT_PORTS[parts.scheme]:
            continue
        # APT appends `/dists/<suite>/...` to the URI as given, so a query or
        # fragment lands in the middle of the path it fetches: a different
        # endpoint wearing the right host and path
        if parts.query or parts.fragment:
            continue
        if posixpath.normpath(parts.path or "/").rstrip("/") == path:
            return True
    return False


#: the lab's nodes are amd64, so that is the architecture whose packages the
#: upgrade needs; a stanza restricted away from it fetches nothing useful
NATIVE_ARCH = "amd64"


def _selects_binaries(stanza: dict[str, list[str]]) -> bool:
    """
    Whether this stanza actually yields binary packages for the node.

    `Targets: Sources` fetches no binaries; so does `Targets: deb`, because
    `deb` is not one of APT's target identifiers -- `Packages` is. Removals and
    architecture restrictions apply on top, and either can leave a stanza that
    reads correctly selecting nothing the node can install.
    """
    targets = [t.lower() for t in stanza.get("targets", [])]
    if targets and "packages" not in targets:
        return False
    if "packages" in [t.lower() for t in stanza.get("targets-remove", [])]:
        return False
    # compared exactly: APT does not fold these, so `Architectures: AMD64`
    # produces `binary-AMD64` targets and no amd64 packages at all
    architectures = stanza.get("architectures", [])
    if architectures and NATIVE_ARCH not in architectures:
        return False
    if NATIVE_ARCH in stanza.get("architectures-remove", []):
        return False
    return True


def _selects_pve_no_subscription(source: str | list) -> bool:
    """
    Whether these entries activate the repository `pveceph install` will use.

    Every field has to hold in the SAME active stanza: a Debian stanza and a
    PVE stanza that between them mention trixie and pve-no-subscription select
    neither.
    """
    stanzas = _deb822_stanzas(source) if isinstance(source, str) else source
    if not _configuration_loads(stanzas):
        return False
    return any(
        _is_enabled(st)
        and "deb" in st.get("types", [])
        and _selects_binaries(st)
        and "trixie" in st.get("suites", [])
        and "pve-no-subscription" in st.get("components", [])
        and _points_at(st, "download.proxmox.com", "/debian/pve")
        for st in stanzas)


def _hosted_at(stanza: dict[str, list[str]], host: str) -> bool:
    """Whether any of the stanza's HTTP(S) URIs is served by *host*."""
    for uri in stanza.get("uris", []):
        parts = urlsplit(uri)
        if parts.scheme not in DEFAULT_PORTS:
            continue
        if _canonical_host(parts.hostname) == host:
            return True
    return False


def _enterprise_is_active(stanzas: list[dict[str, list[str]]]) -> bool:
    """
    Any enabled entry on the subscription-only host.

    Matched by host rather than by one path: the hook disables two of these,
    and the Ceph one is a different repository (`/debian/ceph-squid`) with a
    different component (`enterprise`) from PVE's. Keying on `/debian/pve` saw
    only half of what the hook owns.
    """
    return any(
        _is_enabled(st)
        and (_hosted_at(st, "enterprise.proxmox.com")
             or "pve-enterprise" in st.get("components", []))
        for st in stanzas)


def test_the_upgrade_runs_before_the_guest_agent(box, tmp_path):
    r, marker, events, *_ = _first_boot(box, tmp_path, "    :")
    assert r.returncode == 0, r.stderr
    assert marker.exists(), "a completed hook must leave its marker"
    calls = [ln for ln in events.splitlines() if ln.startswith("apt")]
    upgrade_at = next(i for i, ln in enumerate(calls) if "dist-upgrade" in ln)
    agent_at = next(i for i, ln in enumerate(calls) if "qemu-guest-agent" in ln)
    assert upgrade_at < agent_at, calls


def test_the_marker_is_published_only_after_every_prerequisite(box, tmp_path):
    """
    After all of them, not just after the upgrade.

    wait-first-boot.sh polls this file and treats it as certifying
    repositories, MTU and the guest agent. A hook that published it earlier --
    after the upgrade but before the agent, or before the MTU block -- leaves
    the same filesystem behind and the same call order, and releases the
    cluster build over a node that is not ready. Every prerequisite the hook
    shells out to reports the marker it can see, so this is checked where it
    happens rather than inferred afterwards.
    """
    _r, marker, events, *_ = _first_boot(box, tmp_path, "    :")
    early = [ln for ln in events.splitlines()
             if ln.startswith("MARKER-PRESENT-AT")]
    assert not early, f"the marker was already published during: {early}"
    assert marker.exists()
    # the control: those stubs really did run, so their silence means something
    for step in ("ifreload", "ip", "systemctl"):
        assert f"{step} " in events, f"{step} was never called:\n{events}"


def test_a_failed_upgrade_leaves_the_marker_unwritten(box, tmp_path):
    """
    The whole point of refusing `|| true`.

    An unwritten marker is what makes wait-first-boot.sh fail and name the
    node's log; swallowed, the run would carry on to build a cluster on nodes
    whose PVE does not match the Ceph about to be installed.
    """
    r, marker, *_ = _first_boot(box, tmp_path, STEP_FAILS)
    assert r.returncode != 0, "a failed dist-upgrade reported success"
    assert not marker.exists(), "the marker must not survive a failed upgrade"


def test_a_failed_upgrade_stops_before_the_guest_agent(box, tmp_path):
    # set -e must abort the hook there and then, not run on to the next step
    _r, _marker, events, *_ = _first_boot(box, tmp_path, STEP_FAILS)
    assert "qemu-guest-agent" not in events, events


def test_a_failed_guest_agent_install_leaves_the_marker_unwritten(box, tmp_path):
    # the marker certifies the agent too: boxman discovers a node's IP through it
    r, marker, *_ = _first_boot(box, tmp_path, "    :", install=STEP_FAILS)
    assert r.returncode != 0, "a failed guest-agent install reported success"
    assert not marker.exists()


def test_a_failed_service_enable_leaves_the_marker_unwritten(box, tmp_path):
    r, marker, *_ = _first_boot(box, tmp_path, "    :", systemctl="exit 1")
    assert r.returncode != 0, "a failed systemctl enable reported success"
    assert not marker.exists()


def test_a_failed_mtu_probe_leaves_the_marker_unwritten(box, tmp_path):
    """
    `ip link show vmbr0 | head -1` is the MTU stage's last word, and `pipefail`
    makes its failure the pipeline's. The marker must not follow it.
    """
    r, marker, *_ = _first_boot(box, tmp_path, "    :", ip="exit 1")
    assert r.returncode != 0, "a failed MTU probe reported success"
    assert not marker.exists()


def test_a_failed_reload_keeps_the_hook_s_existing_tolerance(box, tmp_path):
    """
    The counterweight to the test above, and narrower than it looks.

    `ifreload -a || true` has been best-effort since the hook was written:
    ifupdown2 can refuse a reload on a node whose interfaces are already right,
    and making every non-zero reload fatal would change behaviour this PR is
    not changing. That is all this pins.

    It is **not** a claim that the MTU is correct when the marker appears. The
    hook writes the interfaces file, prints `ip link show vmbr0` and never
    reads the value back, so a reload that failed to apply 1450 to the running
    device still publishes the marker and still releases the cluster build --
    a pre-existing gap in the hook, filed separately, not something these tests
    should quietly bless by asserting readiness here.
    """
    r, marker, *_ = _first_boot(box, tmp_path, "    :", ifreload="exit 1")
    assert r.returncode == 0, "a best-effort reload was made fatal"
    assert marker.exists()


def test_the_upgrade_answers_the_conffile_prompt(box, tmp_path):
    """
    Counterfactual: the bare `apt-get dist-upgrade -y -qq` that actually ran on
    the lab on 2026-09-15 fails this. Step 1 of the hook edits
    pve-enterprise.sources, which IS a dpkg conffile, so an upgrade that also
    changes it prompts, and with null stdin that prompt is EOF.
    """
    r, marker, events, hooklog, _ = _first_boot(box, tmp_path, CONFFILE_PROMPT)
    assert r.returncode == 0, \
        hooklog.read_text() if hooklog.exists() else r.stderr
    assert marker.exists(), "the hook stalled on a conffile prompt"
    assert "dist-upgrade" in events


def test_the_upgrade_forwards_both_dpkg_options(box, tmp_path):
    """
    Policy, kept apart from the behaviour above.

    Any one of the force options answers the ordinary conflict, so no
    behavioural test can notice one of the documented pair going missing.
    Assert the pair, parsed out of apt's own option list rather than matched as
    a substring -- a misspelt key reaches no list at all.
    """
    _r, _marker, events, *_ = _first_boot(box, tmp_path, "    :")
    assert sorted(_dpkg_options_of(events)) == \
        ["--force-confdef", "--force-confold"]


def test_the_failure_reaches_the_log_wait_first_boot_names(box, tmp_path):
    """
    wait-first-boot.sh sends the operator to this file. If the hook's `exec`
    stopped redirecting stderr, the file would still hold the banner and the
    operator would still have nothing to read.
    """
    _r, _marker, _events, hooklog, _ = _first_boot(box, tmp_path, STEP_FAILS)
    assert hooklog.exists(), "the hook wrote no log at the advertised path"
    text = hooklog.read_text()
    assert "pve-lab first boot" in text
    assert "PVE_LAB_TEST_APT_BROKE" in text, \
        f"the upgrade's own error never reached the advertised log:\n{text}"


def test_every_apt_update_saw_only_the_intended_repository(box, tmp_path):
    """
    The upgrade is only in step with Ceph if it comes from the repository
    `pveceph install` will use -- and that has to hold at *every* update, not
    just the last one. An extra early update against the stock configuration
    would otherwise be hidden by the correct state captured afterwards.

    Read from the sources as apt saw them, across every file APT would read,
    and parsed rather than grepped: a file that exists, one whose fields are
    spread over two stanzas, or one APT would refuse to load, is not a
    configured repository.
    """
    _r, _marker, events, _hooklog, snapshot = _first_boot(box, tmp_path, "    :")
    updates = sorted(d for d in snapshot.iterdir() if d.is_dir())
    assert updates, f"apt never reached an update:\n{events}"

    # The snapshot can only stand for what apt read if the hook does not move
    # the goalposts: a symlinked source can be rewritten after the copy, and
    # a `Dir::Etc` override sends apt to a directory nothing captured. Neither
    # is something this hook does; asserted rather than assumed, because the
    # capture silently certifies the wrong input otherwise.
    assert "SYMLINKED-SOURCE" not in events, \
        f"a source file was a symlink, so what apt read is not frozen:\n{events}"
    for update in updates:
        _assert_update_is_the_replayed_one(update)

    for update in updates:
        stanzas = _apt_stanzas(update)
        assert _configuration_loads(stanzas), \
            f"APT could not have loaded the sources at {update.name}"
        assert _selects_pve_no_subscription(stanzas), \
            f"no active pve-no-subscription entry at {update.name}"
        assert not _enterprise_is_active(stanzas), \
            f"the enterprise repository was still active at {update.name}"


#: what APT may append to an index name
COMPRESSION_SUFFIXES = (".xz", ".gz", ".bz2", ".lzma", ".lz4", ".zst")

#: the index the node's upgrade needs, as a path rather than a spelling
INTENDED_PATH = (f"/debian/pve/dists/trixie/pve-no-subscription"
                 f"/binary-{NATIVE_ARCH}/Packages")


def _canonical_host(host: str) -> str:
    """
    Fold case and drop the single terminal dot of a fully qualified name.

    Exactly one: `example.com.` is `example.com`, while `example.com..` is
    neither -- and treating it as the same host let a URL past the check that
    exists to refuse it.
    """
    host = (host or "").lower()
    return host[:-1] if host.endswith(".") else host


def _served_by(url: str, host: str) -> bool:
    """
    Whether *url*'s parsed hostname is *host*, whatever its case.

    The trailing dot of a fully qualified name is stripped: `example.com.` and
    `example.com` are the same host, and leaving it in let a subscription URL
    past the very check that exists to refuse it.
    """
    return _canonical_host(urlsplit(url).hostname) == host


def _is_native_packages_url(url: str) -> bool:
    """
    Whether *url* is the intended repository's native Packages index.

    Parsed, not prefix-matched. A literal prefix rejects `https`, an uppercase
    hostname and an explicit `:80` -- all of which APT resolves to the same
    endpoint and which are supported spellings for this hook -- while a pair of
    substrings would accept the same path served by any other host.
    """
    parts = urlsplit(url)
    if parts.scheme not in DEFAULT_PORTS:
        return False
    if not _served_by(url, "download.proxmox.com"):
        return False
    if parts.port is not None and parts.port != DEFAULT_PORTS[parts.scheme]:
        return False
    if parts.query or parts.fragment:
        return False
    path = posixpath.normpath(parts.path or "/")
    # the file itself, bare or compressed -- not everything under a *directory*
    # whose name happens to begin `Packages.`
    return path in {INTENDED_PATH, *(INTENDED_PATH + suffix
                                     for suffix in COMPRESSION_SUFFIXES)}


def _assert_update_is_the_replayed_one(update_dir) -> None:
    """
    The replay runs a plain `apt-get update`, so it stands for the hook's own
    invocation only while that is plain too.

    Two ways it might not be, both of which leave the captured sources looking
    right: an option the replay does not reproduce -- `-c`, `APT::Architecture`,
    a disabled Packages target -- or an `APT_CONFIG` the hook exported, which
    APT reads before anything on the command line. Argument boundaries are
    compared, not text: `update update -qq`, `update -qq ""` and
    `update "-qq -qq"` are all rejected by APT and all look the same once
    flattened and split.

    Deliberately narrow, and fail-closed: a legitimate new option on the hook's
    update line fails here, which is the signal to revisit this fixture.
    """
    raw = (update_dir / "argv0").read_bytes().decode()
    args = raw.split("\0")[:-1] if raw.endswith("\0") else raw.split("\0")
    assert args == ["update", "-qq"], \
        f"unsupported apt-get update invocation, which the replay does not " \
        f"reproduce: {args!r}"
    hook_config = (update_dir / "apt_config").read_text()
    assert hook_config == "", \
        f"the hook set APT_CONFIG={hook_config!r}; APT reads it before every " \
        f"other source of configuration, so what it selected is not what " \
        f"this snapshot describes"
    present = update_dir / "aptconf-present"
    named = present.read_text().split() if present.exists() else []
    written = [path.name for path in (update_dir / "aptconf").rglob("*")
               if path.is_file()]
    assert not named and not written, \
        f"the hook wrote APT configuration that the replay does not " \
        f"reproduce: {sorted(set(named + written))}"


def _apt_enumerates(update_dir):
    """
    Ask APT itself which indexes it would fetch for one captured update.

    Returns ``(exit_status, [url, ...], stderr)``, or ``None`` where APT is not
    installed. ``--print-uris`` resolves targets without downloading anything.

    The isolation is a private bootstrap handed over ``APT_CONFIG``, not a set
    of late ``-o`` flags: APT reads that file *before* `/etc/apt/apt.conf` and
    `/etc/apt/apt.conf.d/*`, so disabling those there is the only way to stop
    the host's own settings deciding the answer. Without it this test depended
    on the machine running it -- an ambient `APT::Architecture` or a
    `DefaultEnabled "false"` silently changed which targets APT reported for
    identical source bytes.
    """
    apt = shutil.which("apt-get")
    if apt is None:
        return None
    private = update_dir / "apt-run"
    for sub in ("state/lists/partial", "cache/archives/partial", "log"):
        (private / sub).mkdir(parents=True, exist_ok=True)
    main = update_dir / "sources.list"
    bootstrap = private / "bootstrap.conf"
    bootstrap.write_text(f'''
Dir::Etc::main "/dev/null";
Dir::Etc::parts "/dev/null";
Dir::Etc::sourcelist "{main if main.exists() else '/dev/null'}";
Dir::Etc::sourceparts "{update_dir / 'parts'}";
Dir::Etc::trusted "/dev/null";
Dir::Etc::trustedparts "/dev/null";
Dir::State "{private / 'state'}";
Dir::Cache "{private / 'cache'}";
Dir::Log "{private / 'log'}";
APT::Architecture "{NATIVE_ARCH}";
APT::Architectures {{ "{NATIVE_ARCH}"; }};
Acquire::AllowInsecureRepositories "true";
quiet "0";
''')

    out = subprocess.run(
        [apt, "--print-uris", "update"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "APT_CONFIG": str(bootstrap), "LC_ALL": "C"})
    # each record is `'<uri>' <filename> <size> <hash>`; a bare scan for quoted
    # text tears a URI containing an apostrophe into pieces
    urls = [m.group(1) for m in
            (re.match(r"^'(.+)'\s+\S+\s+\d+\s", line)
             for line in out.stdout.splitlines()) if m]
    return out.returncode, urls, out.stderr


@pytest.mark.skipif(shutil.which("apt-get") is None,
                    reason="needs APT to enumerate targets")
def test_apt_itself_selects_only_the_intended_repository(box, tmp_path):
    """
    The same claim as the test above, answered by the program that decides it.

    Every intercepted update is replayed through `apt-get --print-uris`, which
    fails outright on a configuration APT will not load and otherwise names the
    exact indexes it would fetch. A node's upgrade is in step with Ceph only if
    those include a pve-no-subscription Packages index for its architecture and
    none from the subscription host.
    """
    _r, _marker, events, _hooklog, snapshot = _first_boot(box, tmp_path, "    :")
    updates = sorted(d for d in snapshot.iterdir() if d.is_dir())
    assert updates, f"apt never reached an update:\n{events}"

    for update in updates:
        _assert_update_is_the_replayed_one(update)
        status, urls, errors = _apt_enumerates(update)
        assert status == 0, \
            f"APT could not load the sources at {update.name}: {errors}"
        wanted = [u for u in urls if _is_native_packages_url(u)]
        assert wanted, \
            f"APT fetches no pve-no-subscription packages at {update.name}:\n" \
            + "\n".join(urls)
        enterprise = [u for u in urls if _served_by(u, "enterprise.proxmox.com")]
        assert not enterprise, \
            f"APT still fetches from the subscription host: {enterprise}"


#: Formats a working configuration can legitimately take, and selections that
#: only look like one. The parser decides the repository test's verdict, so it
#: gets its own controls rather than being trusted. The expectations are the
#: ones real APT was observed to have.
GOOD = """\
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
"""

SOURCE_VARIANTS = [
    ("plain", GOOD, True),
    ("tabs-after-colon", GOOD.replace(": ", ":\t"), True),
    ("no-space-after-colon", GOOD.replace(": ", ":"), True),
    ("crlf-line-endings", GOOD.replace("\n", "\r\n"), True),
    ("continuation-lines",
     "Types: deb\nURIs:\n http://download.proxmox.com/debian/pve\n"
     "Suites:\n trixie\nComponents:\n pve-no-subscription\n", True),
    ("extra-fields", GOOD + "Architectures: amd64\nX-Extra: something\n", True),
    ("two-uris-one-line",
     GOOD.replace("URIs: http://download.proxmox.com/debian/pve",
                  "URIs: https://download.proxmox.com/debian/pve"
                  " http://download.proxmox.com/debian/pve"), True),
    ("trailing-slash-uri", GOOD.replace("/debian/pve", "/debian/pve/"), True),
    ("uppercase-host",
     GOOD.replace("download.proxmox.com", "DOWNLOAD.PROXMOX.COM"), True),
    ("explicit-default-port",
     GOOD.replace("download.proxmox.com", "download.proxmox.com:80"), True),
    ("types-deb-and-deb-src", GOOD.replace("Types: deb", "Types: deb deb-src"),
     True),
    ("absolute-signed-by",
     GOOD + "Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n",
     True),
    ("plus-a-disabled-unrelated-stanza",
     GOOD + "\nTypes: deb\nURIs: http://example.invalid/x\nSuites: trixie\n"
            "Components: main\nEnabled: false\n", True),
    # APT reads an unrecognised Enabled value as enabled, so neither does this
    # count as disabled
    ("enabled-is-not-exactly-false",
     GOOD + "Enabled: false but not exactly false\n", True),
    ("enabled-off-extra", GOOD + "Enabled: off extra\n", True),
    ("disabled-with-no", GOOD + "Enabled: no\n", False),
    ("disabled-with-false", GOOD + "Enabled: false\n", False),
    ("unknown-type", GOOD.replace("Types: deb", "Types: deb invalid-type"),
     False),
    ("malformed-signed-by", GOOD + "Signed-By: not-a-key\n", False),
    ("file-scheme", GOOD.replace("http://", "file://"), False),
    ("non-default-port",
     GOOD.replace("download.proxmox.com", "download.proxmox.com:8080"), False),
    ("fields-split-across-stanzas",
     "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\n"
     "Components: main\n\nTypes: deb\n"
     "URIs: http://download.proxmox.com/debian/pve\nSuites: bookworm\n"
     "Components: pve-no-subscription\n", False),
    ("different-uri-path",
     GOOD.replace("/debian/pve", "/debian/pve-not-a-repository"), False),
    ("only-a-comment", "# No package repositories configured\n", False),
    ("no-suites", GOOD.replace("Suites: trixie\n", ""), False),
    ("no-uris", GOOD.replace("URIs: http://download.proxmox.com/debian/pve\n", ""),
     False),
    ("no-components", GOOD.replace("Components: pve-no-subscription\n", ""),
     False),
    ("colonless-line", GOOD + "this line has no colon\n", False),
    ("fingerprint-signed-by",
     GOOD + "Signed-By: " + "A" * 40 + "\n", True),
    ("fingerprint-with-exclamation",
     GOOD + "Signed-By: " + "A" * 40 + "!\n", True),
    ("forty-one-hex-digits", GOOD + "Signed-By: " + "A" * 41 + "\n", False),
    ("path-then-invalid-token",
     GOOD + "Signed-By: /usr/share/keyrings/k.gpg not-a-key\n", False),
    ("targets-sources-only", GOOD + "Targets: Sources\n", False),
    ("targets-packages", GOOD + "Targets: Packages\n", True),
]


@pytest.mark.parametrize("source,selects",
                         [(v[1], v[2]) for v in SOURCE_VARIANTS],
                         ids=[v[0] for v in SOURCE_VARIANTS])
def test_the_source_reader_handles_what_this_hook_can_write(source, selects):
    """
    The scope of the model, stated rather than implied.

    These expectations were each checked against real APT 2.7.14, and the
    reader exists to answer one question about one hook: did this first boot
    leave an active pve-no-subscription source and no active enterprise entry?
    It is deliberately not a general APT validator -- it does not model
    conflicting options, trust settings, or acquisition, and an earlier name
    claiming it "agrees with APT" was an overclaim that successive reviews
    kept falsifying.
    """
    assert _selects_pve_no_subscription(source) is selects


ONE_LINE_VARIANTS = [
    ("no-subscription-in-a-list-file",
     "deb http://download.proxmox.com/debian/pve trixie pve-no-subscription\n",
     True, False),
    ("with-options",
     "deb [signed-by=/usr/share/keyrings/k.gpg] "
     "http://download.proxmox.com/debian/pve trixie pve-no-subscription\n",
     True, False),
    ("enterprise-in-a-list-file",
     "deb https://enterprise.proxmox.com/debian/pve trixie pve-enterprise\n",
     False, True),
    ("commented-out",
     "# deb http://download.proxmox.com/debian/pve trixie pve-no-subscription\n",
     False, False),
    ("an-unrelated-debian-source",
     "deb http://deb.debian.org/debian trixie main\n", False, False),
    # the component is commented out, so it is not a component
    ("component-after-a-comment",
     "deb http://download.proxmox.com/debian/pve trixie main"
     " # pve-no-subscription\n", False, False),
    ("unknown-type-poisons-the-file",
     "invalid-type http://download.proxmox.com/debian/pve trixie"
     " pve-no-subscription\n", False, False),
    ("bad-signed-by-option",
     "deb [signed-by=not-a-key] http://download.proxmox.com/debian/pve"
     " trixie pve-no-subscription\n", False, False),
    # `[arch=...]` restricts the entry exactly as `Architectures:` does
    ("arch-restricted-away-from-native",
     "deb [arch=arm64] http://download.proxmox.com/debian/pve trixie"
     " pve-no-subscription\n", False, False),
    ("arch-includes-native",
     "deb [arch=amd64] http://download.proxmox.com/debian/pve trixie"
     " pve-no-subscription\n", True, False),
    ("dotted-enterprise-host",
     "deb http://enterprise.proxmox.com./debian/ceph-squid trixie"
     " enterprise\n", False, True),
]


#: What APT enumerates, and what only looks like it. The index is a file: a
#: flat repository rooted at a *directory* called `Packages.backup` produces
#: URLs that begin the same way and fetch something else entirely.
_BASE = "http://download.proxmox.com/debian/pve/dists/trixie/pve-no-subscription"
NATIVE_URL_CASES = [
    ("bare", f"{_BASE}/binary-amd64/Packages", True),
    ("compressed", f"{_BASE}/binary-amd64/Packages.xz", True),
    ("https", "https://download.proxmox.com/debian/pve/dists/trixie"
              "/pve-no-subscription/binary-amd64/Packages.gz", True),
    ("uppercase-host", f"{_BASE}/binary-amd64/Packages".replace(
        "download.proxmox.com", "DOWNLOAD.PROXMOX.COM"), True),
    ("default-port", f"{_BASE}/binary-amd64/Packages".replace(
        "download.proxmox.com", "download.proxmox.com:80"), True),
    ("dotted-host", f"{_BASE}/binary-amd64/Packages".replace(
        "download.proxmox.com", "download.proxmox.com."), True),
    # one terminal dot is the fully qualified spelling; two is a different,
    # malformed name and not this repository
    ("double-dotted-host", f"{_BASE}/binary-amd64/Packages".replace(
        "download.proxmox.com", "download.proxmox.com.."), False),
    ("triple-dotted-host", f"{_BASE}/binary-amd64/Packages".replace(
        "download.proxmox.com", "download.proxmox.com..."), False),
    ("backup-directory", f"{_BASE}/binary-amd64/Packages.backup/InRelease",
     False),
    ("compressed-directory", f"{_BASE}/binary-amd64/Packages.xz/Packages.xz",
     False),
    ("wrong-architecture", f"{_BASE}/binary-arm64/Packages", False),
    ("wrong-component", f"{_BASE}/../pve-enterprise/binary-amd64/Packages",
     False),
    ("other-host", f"{_BASE}/binary-amd64/Packages".replace(
        "download.proxmox.com", "mirror.invalid"), False),
    ("non-default-port", f"{_BASE}/binary-amd64/Packages".replace(
        "download.proxmox.com", "download.proxmox.com:8080"), False),
    ("query", f"{_BASE}/binary-amd64/Packages?mirror=1", False),
    ("fragment", f"{_BASE}/binary-amd64/Packages#packages", False),
    ("translation-index", f"{_BASE}/i18n/Translation-en.xz", False),
]


@pytest.mark.parametrize("url,is_native",
                         [(c[1], c[2]) for c in NATIVE_URL_CASES],
                         ids=[c[0] for c in NATIVE_URL_CASES])
def test_the_native_index_is_recognised_by_structure(url, is_native):
    assert _is_native_packages_url(url) is is_native


@pytest.mark.parametrize("text,selects,enterprise",
                         [(v[1], v[2], v[3]) for v in ONE_LINE_VARIANTS],
                         ids=[v[0] for v in ONE_LINE_VARIANTS])
def test_one_line_sources_are_read_too(text, selects, enterprise):
    """
    APT reads `.list` and `.sources` from the same directory. An enterprise
    entry in the format the test does not parse is still an enterprise entry.
    """
    entries = _one_line_entries(text)
    assert _selects_pve_no_subscription(entries) is selects
    assert _enterprise_is_active(entries) is enterprise
