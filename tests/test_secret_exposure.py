"""
Secret- and trust-exposure tests (#164 CL-S1, CL-S2, CL-S3).

Each of these pins a property that is invisible in normal use and only
shows up when someone else is looking: a password in a process list, a
world-readable file, a host-key check silently turned off for hosts that
have nothing to do with boxman.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager
from boxman.runtime.docker_compose import docker_exec_wrap

pytestmark = pytest.mark.unit

ADMIN_PASS = "s3cret pass$(touch /tmp/pwned)"


def _manager(tmp_path, runtime_name="local"):
    """A manager with two VMs whose IPs are already known."""
    mgr = BoxmanManager()
    mgr.config = {
        "project": "test-proj",
        "workspace": {"path": str(tmp_path)},
        "clusters": {
            "cluster_1": {
                "workdir": str(tmp_path / "cluster_1"),
                "admin_user": "admin",
                "admin_key_name": "id_ed25519_boxman",
                "ssh_config": "ssh_config",
                "vms": {"vm1": {"hostname": "vm1"}},
            },
        },
    }
    mgr._runtime_name = runtime_name
    mgr.provider = MagicMock()
    mgr.provider.get_vm_ip_addresses.side_effect = (
        lambda full_name: {"vnet0": "192.168.11.91"})
    return mgr


def _effective(config_path, host):
    """What OpenSSH itself would apply to *host* — not what the file says."""
    out = subprocess.run(
        ["ssh", "-F", str(config_path), "-G", host],
        capture_output=True, text=True, check=True).stdout
    return dict(
        line.split(" ", 1) for line in out.splitlines() if " " in line)


needs_ssh = pytest.mark.skipif(shutil.which("ssh") is None,
                               reason="the oracle here is OpenSSH itself")

#: what `ssh -G` prints for a disabled check. OpenSSH 10 normalises `no` to
#: `false`, so asserting `!= "no"` would pass on a config that *had* disabled
#: it -- the whole set is compared instead.
DISABLED = {"no", "false", "off"}


class TestHostKeyCheckingIsScoped:
    """
    CL-S1: the file opened with `Host *` + `StrictHostKeyChecking no`, and
    it is meant to be `Include`d. OpenSSH takes the first value it sees for
    a keyword, so that stanza disabled host-key checking for every host the
    reader ever connected to -- github.com included.
    """

    @needs_ssh
    def test_an_unrelated_host_keeps_its_host_key_checking(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr.write_ssh_config()
        effective = _effective(tmp_path / "ssh_config", "github.com")
        assert effective["stricthostkeychecking"] not in DISABLED
        assert effective["userknownhostsfile"] != "/dev/null"

    @needs_ssh
    def test_a_boxman_vm_still_skips_it(self, tmp_path):
        # not a leftover: a VM is recreated often enough that its host key
        # changes under a reused IP, so the relaxation is deliberate *here*
        mgr = _manager(tmp_path)
        mgr.write_ssh_config()
        effective = _effective(tmp_path / "ssh_config", "cluster_1_vm1")
        assert effective["stricthostkeychecking"] in DISABLED
        assert effective["userknownhostsfile"] == "/dev/null"

    def test_no_wildcard_stanza_is_written(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr.write_ssh_config()
        content = (tmp_path / "ssh_config").read_text()
        assert "Host *" not in content


class TestThePasswordStaysOffTheArgv:
    """
    CL-S2: `sshpass -p <password>` put it in the process list, readable by
    every user on the host, for each of up to ten attempts per VM.
    """

    @staticmethod
    def _capture(mgr, tmp_path):
        with patch("boxman.manager_parts.ssh.run") as fake_run:
            fake_run.return_value = MagicMock(ok=False, stdout="", stderr="")
            with patch.object(type(mgr), "_verify_ssh_connection",
                              return_value=True, create=True), \
                 patch("boxman.manager_parts.ssh.time.sleep"):
                mgr._try_add_ssh_key(
                    ip_address="192.168.11.91", hostname="vm1",
                    admin_user="admin", admin_pass=ADMIN_PASS,
                    pub_key_path=str(tmp_path / "a key.pub"),
                    ssh_conf_path=str(tmp_path / "ssh_config"))
        return fake_run.call_args_list

    def test_no_call_carries_the_password_in_its_command(self, tmp_path):
        mgr = _manager(tmp_path)
        for call in self._capture(mgr, tmp_path):
            assert ADMIN_PASS not in call.args[0], call.args[0]
            assert "sshpass -e" in call.args[0]

    def test_the_password_travels_in_the_environment(self, tmp_path):
        mgr = _manager(tmp_path)
        for call in self._capture(mgr, tmp_path):
            assert call.kwargs["env"]["SSHPASS"] == ADMIN_PASS

    def test_the_remaining_arguments_are_quoted(self, tmp_path):
        # a path with a space used to split into two arguments
        mgr = _manager(tmp_path)
        quoted = shlex.quote(str(tmp_path / "a key.pub"))
        assert " " in quoted, "the fixture has to need quoting to prove anything"
        for call in self._capture(mgr, tmp_path):
            assert quoted in call.args[0], call.args[0]


class TestTheContainerGetsTheSecretOutOfBand:

    def test_docker_exec_forwards_the_name_not_the_value(self):
        # `-e NAME` is docker's spelling for "take it from my environment",
        # so the value is on neither argv
        wrapped = docker_exec_wrap("sshpass -e ssh-copy-id x", "c1",
                                   pass_env=("SSHPASS",))
        assert "-e SSHPASS " in wrapped
        assert "SSHPASS=" not in wrapped

    def test_without_a_passthrough_nothing_changes(self):
        assert docker_exec_wrap("virsh list", "c1") == \
            docker_exec_wrap("virsh list", "c1", pass_env=())


class TestTheRenderedConfigIsNotWorldReadable:
    """
    CL-S3: every `env()` is resolved in `conf.rendered.yml`, and the shipped
    boxes render `admin_pass: {{ env("BOXMAN_ADMIN_PASS") }}` into it. It
    was written 0644 on every command.
    """

    @staticmethod
    def _render(tmp_path):
        (tmp_path / "conf.yml").write_text(
            'project: p\nclusters:\n  c1:\n    workdir: /tmp/x\n'
            '    admin_pass: "{{ env(\'BOXMAN_ADMIN_PASS\') }}"\n')
        mgr = BoxmanManager()
        with patch.dict(os.environ, {"BOXMAN_ADMIN_PASS": "hunter2"}):
            mgr.load_config(str(tmp_path / "conf.yml"))
        return tmp_path / "conf.rendered.yml"

    def test_a_new_file_is_private(self, tmp_path):
        rendered = self._render(tmp_path)
        assert "hunter2" in rendered.read_text(), "the secret is really in it"
        assert stat.S_IMODE(rendered.stat().st_mode) == 0o600

    def test_one_left_behind_by_an_older_boxman_is_tightened(self, tmp_path):
        # O_CREAT's mode applies only to a file being created, so an
        # existing 0644 would otherwise stay 0644 forever
        stale = tmp_path / "conf.rendered.yml"
        stale.write_text("old\n")
        stale.chmod(0o644)
        assert stat.S_IMODE(self._render(tmp_path).stat().st_mode) == 0o600
