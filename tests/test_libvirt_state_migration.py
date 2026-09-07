"""
Tests for the persisted libvirt state of the docker-compose runtime
(#164 FB-2) and the unified ``BOXMAN_DATA_DIR`` derivation (#164 FB-11).

FB-2: ``/etc/libvirt`` and ``/var/lib/libvirt/qemu`` used to live in the
container's writable layer, so every container recreate discarded every
domain, network, snapshot and NVRAM file. They are bind-mounted now, and a
container that predates the mounts has its state migrated out before
anything stops it.

FB-11: with a user-supplied compose file the process environment derived
``BOXMAN_DATA_DIR`` from the compose file's directory while ``.env``
derived it from the runtime dir, so the container mounted one directory
and every host-side consumer looked in the other.
"""

import io
import os
import shlex
import subprocess
import tarfile
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ProvisionError
from boxman.runtime.docker_compose import DockerComposeRuntime

CONTAINER = "boxman-libvirt-proj"


def _runtime(tmp_path, **config):
    rt = DockerComposeRuntime(config={"runtime_container": CONTAINER,
                                      "ready_timeout": 1, **config})
    rt.project_dir = str(tmp_path / "proj")
    os.makedirs(rt.project_dir, exist_ok=True)
    return rt


def _make_tar(path, root, entries):
    """Write the kind of archive ``docker cp <c>:/a/<root> -`` produces."""
    with tarfile.open(path, "w") as tar:
        top = tarfile.TarInfo(root)
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, payload in entries:
            info = tarfile.TarInfo(f"{root}/{name}")
            info.mode = 0o644
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


#: What the two trees hold, as far as these tests care.
STATE_FIXTURES = {
    "/etc/libvirt": ("libvirt", [
        ("libvirtd.conf", b"listen_tls = 0\n"),
        ("qemu/vm1.xml", b"<domain><name>vm1</name></domain>\n"),
    ]),
    "/var/lib/libvirt/qemu": ("qemu", [
        ("nvram/vm1_VARS.fd", b"\0" * 256),
        ("save/vm1.save", b"saved-state"),
    ]),
}


def _redirect_target(command):
    """The file a ``... > path`` shell redirection writes to."""
    _, _, target = command.rpartition(">")
    return shlex.split(target.strip())[0]


def _dispatch(rt, *, state="running", guests=0, persisted=False,
              truncate=(), calls=None):
    """An ``invoke.run`` stand-in covering the migration's shell-outs."""
    import json as _json

    def _run(command, *_args, **_kwargs):
        if calls is not None:
            calls.append(command)
        if "docker ps -a" in command:
            return MagicMock(ok=True, stdout=f"{state}\n" if state else "")
        if rt._GUEST_PROBE_MARKER in command:
            return MagicMock(
                ok=True, stdout=f"{rt._GUEST_PROBE_MARKER}{guests}\n")
        if "docker inspect" in command and ".Mounts" in command:
            if not state:
                return MagicMock(ok=False, stdout="")
            mounts = []
            if persisted:
                mounts = [{"Source": rt._state_host_dir(sub),
                           "Destination": path, "RW": True}
                          for sub, path in rt._PERSISTED_STATE]
            return MagicMock(ok=True, stdout=_json.dumps(mounts))
        if command.startswith("docker cp"):
            source = command.split()[2].split(":", 1)[1]
            root, entries = STATE_FIXTURES[source]
            target = _redirect_target(command)
            _make_tar(target, root, entries)
            if source in truncate:
                data = open(target, "rb").read()
                with tarfile.open(target) as tar:
                    last = tar.getmembers()[-1]
                end = last.offset_data + ((last.size + 511) // 512) * 512
                with open(target, "wb") as fobj:
                    fobj.write(data[:end])
            return MagicMock(ok=True, stdout="", stderr="")
        return MagicMock(ok=True, stdout="", stderr="")

    return _run


class TestGuestProbeScript:
    """The probe's shell is executed, not just pattern-matched."""

    @staticmethod
    def _run_probe(tmp_path, body):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(exist_ok=True)
        pgrep = fake_bin / "pgrep"
        pgrep.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        pgrep.chmod(0o755)
        # /bin/sh by absolute path: PATH is narrowed to the fake bin so
        # the script's own pgrep lookup is controlled, which would
        # otherwise hide the shell too.
        return subprocess.run(
            ["/bin/sh", "-c", DockerComposeRuntime._GUEST_PROBE_SCRIPT],
            capture_output=True, text=True,
            env={"PATH": str(fake_bin)},
        )

    def test_reports_the_count_when_pgrep_matches(self, tmp_path):
        result = self._run_probe(tmp_path, 'echo 2\nexit 0\n')
        assert result.returncode == 0
        assert "BOXMAN_GUEST_PROBE:2" in result.stdout

    def test_reports_zero_when_pgrep_finds_nothing(self, tmp_path):
        result = self._run_probe(tmp_path, 'echo 0\nexit 1\n')
        assert result.returncode == 0
        assert "BOXMAN_GUEST_PROBE:0" in result.stdout

    @pytest.mark.parametrize("status", [2, 3, 127])
    def test_a_probe_that_did_not_complete_prints_nothing(self, tmp_path,
                                                          status):
        """The failure that would authorise destroying a live guest.

        Nothing used to check pgrep's status, so a broken or missing pgrep
        left awk with no input, awk printed 0, and boxman read a confident
        "no guests".
        """
        result = self._run_probe(tmp_path, f'exit {status}\n')
        assert result.returncode == 9
        assert "BOXMAN_GUEST_PROBE" not in result.stdout

    def test_missing_pgrep_prints_nothing(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        result = subprocess.run(
            ["/bin/sh", "-c", DockerComposeRuntime._GUEST_PROBE_SCRIPT],
            capture_output=True, text=True, env={"PATH": str(empty)})
        assert result.returncode == 9
        assert "BOXMAN_GUEST_PROBE" not in result.stdout

    def test_matches_by_process_name_not_command_line(self, tmp_path):
        """``pgrep -f`` would match the probe's own shell.

        The pattern appears in the wrapper's command line, so a ``-f``
        query counts the wrapper and every empty runtime reports a guest —
        the opposite failure to the one above, and just as wrong.
        """
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        argv_log = tmp_path / "argv"
        (fake_bin / "pgrep").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$@" > {argv_log}\necho 0\nexit 1\n')
        (fake_bin / "pgrep").chmod(0o755)
        subprocess.run(
            ["/bin/sh", "-c", DockerComposeRuntime._GUEST_PROBE_SCRIPT],
            capture_output=True, env={"PATH": str(fake_bin)})
        argv = argv_log.read_text().split("\n")
        assert "-f" not in argv
        assert "^qemu-(kvm|system-)" in argv

    def test_pattern_covers_both_emulator_names(self):
        """The image ships qemu-kvm *and* qemu-system-x86_64.

        ``comm`` truncates to 15 characters, so the second appears as
        ``qemu-system-x86``; the pattern anchors at the front so it matches
        without needing the tail.
        """
        import re
        pattern = re.compile("^qemu-(kvm|system-)")
        assert pattern.match("qemu-kvm")
        assert pattern.match("qemu-system-x86")
        assert not pattern.match("qemu-img")
        assert not pattern.match("sh")


class TestRunningGuestCount:

    def test_parses_the_marker(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", side_effect=_dispatch(rt, guests=3)):
            assert rt._running_guest_count() == 3

    def test_exec_failure_is_unknown_not_zero(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", return_value=MagicMock(ok=False, stdout="")):
            assert rt._running_guest_count() is None

    def test_missing_marker_is_unknown_not_zero(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run",
                   return_value=MagicMock(ok=True, stdout="0\n")):
            assert rt._running_guest_count() is None

    def test_unparsable_count_is_unknown(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", return_value=MagicMock(
                ok=True, stdout="BOXMAN_GUEST_PROBE:lots\n")):
            assert rt._running_guest_count() is None

    def test_a_raising_shell_is_unknown(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", side_effect=OSError("docker gone")):
            assert rt._running_guest_count() is None


class TestContainerState:

    def test_absent_container_is_empty_not_none(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", return_value=MagicMock(ok=True, stdout="\n")):
            assert rt._container_state() == ""

    def test_docker_failure_is_unknown(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", return_value=MagicMock(ok=False, stdout="")):
            assert rt._container_state() is None

    def test_reports_the_state(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run",
                   return_value=MagicMock(ok=True, stdout="paused\n")):
            assert rt._container_state() == "paused"


class TestRecreateRefusal:

    def test_running_guests_refuse(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", side_effect=_dispatch(rt, guests=2)):
            with pytest.raises(ProvisionError, match="2 QEMU guest"):
                rt._assert_no_running_guests("recreate the container")

    def test_unknown_guest_state_refuses(self, tmp_path):
        rt = _runtime(tmp_path)

        def _run(command, *_a, **_kw):
            if "docker ps -a" in command:
                return MagicMock(ok=True, stdout="running\n")
            return MagicMock(ok=False, stdout="")

        with patch("invoke.run", side_effect=_run):
            with pytest.raises(ProvisionError, match="could not determine"):
                rt._assert_no_running_guests("recreate the container")

    def test_paused_container_refuses(self, tmp_path):
        """docker exec cannot probe a paused container, and its qemu
        processes are frozen rather than gone."""
        rt = _runtime(tmp_path)
        with patch("invoke.run",
                   return_value=MagicMock(ok=True, stdout="paused\n")):
            with pytest.raises(ProvisionError, match="'paused'"):
                rt._assert_no_running_guests("recreate the container")

    def test_unknown_container_existence_refuses(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", return_value=MagicMock(ok=False, stdout="")):
            with pytest.raises(ProvisionError, match="could not determine"):
                rt._assert_no_running_guests("recreate the container")

    @pytest.mark.parametrize("state", ["exited", "created", "dead"])
    def test_states_without_guests_are_allowed(self, tmp_path, state):
        rt = _runtime(tmp_path)
        with patch("invoke.run", side_effect=_dispatch(rt, state=state)):
            rt._assert_no_running_guests("recreate the container")

    def test_absent_container_is_allowed(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", side_effect=_dispatch(rt, state="")):
            rt._assert_no_running_guests("recreate the container")

    def test_no_guests_is_allowed(self, tmp_path):
        rt = _runtime(tmp_path)
        with patch("invoke.run", side_effect=_dispatch(rt, guests=0)):
            rt._assert_no_running_guests("recreate the container")

    def test_allow_recreate_overrides(self, tmp_path):
        rt = _runtime(tmp_path)
        rt.allow_recreate = True
        with patch("invoke.run", side_effect=_dispatch(rt, guests=2)):
            rt._assert_no_running_guests("recreate the container")


class TestMigration:

    def _migrate(self, rt, **kwargs):
        compose = str(tmp_compose(rt))
        with patch("invoke.run",
                   side_effect=_dispatch(rt, **kwargs)):
            rt._ensure_state_persisted(compose, os.path.dirname(compose))

    def test_state_is_copied_out_and_marked(self, tmp_path):
        rt = _runtime(tmp_path)
        self._migrate(rt)

        data = rt._data_dir()
        assert open(os.path.join(data, "etc-libvirt/libvirtd.conf")).read() \
            == "listen_tls = 0\n"
        assert os.path.isfile(
            os.path.join(data, "etc-libvirt/qemu/vm1.xml"))
        assert os.path.isfile(
            os.path.join(data, "var-lib-libvirt-qemu/nvram/vm1_VARS.fd"))
        assert os.path.isfile(
            os.path.join(data, "var-lib-libvirt-qemu/save/vm1.save"))
        assert os.path.isfile(rt._state_marker_path())

    def test_staging_directories_are_cleaned_up(self, tmp_path):
        rt = _runtime(tmp_path)
        self._migrate(rt)
        for subdir, _ in rt._PERSISTED_STATE:
            assert not os.path.exists(
                rt._state_host_dir(subdir) + rt._STAGING_SUFFIX)

    def test_the_container_is_stopped_never_downed(self, tmp_path):
        """``down`` removes the writable layer this copy reads from."""
        rt = _runtime(tmp_path)
        calls = []
        compose = str(tmp_compose(rt))
        with patch("invoke.run", side_effect=_dispatch(rt, calls=calls)):
            rt._ensure_state_persisted(compose, os.path.dirname(compose))
        compose_calls = [c for c in calls if "compose" in c]
        assert any(c.rstrip().endswith(" stop") for c in compose_calls)
        assert not any(c.rstrip().endswith(" down") for c in compose_calls)

    def test_a_truncated_copy_refuses_and_leaves_no_marker(self, tmp_path):
        rt = _runtime(tmp_path)
        calls = []
        with pytest.raises(ProvisionError, match="did not complete"):
            self._migrate(rt, truncate=("/var/lib/libvirt/qemu",),
                          calls=calls)
        assert not os.path.isfile(rt._state_marker_path())
        assert not any(c.rstrip().endswith(" down") for c in calls)

    def test_a_truncated_copy_can_be_retried(self, tmp_path):
        rt = _runtime(tmp_path)
        with pytest.raises(ProvisionError):
            self._migrate(rt, truncate=("/var/lib/libvirt/qemu",))
        self._migrate(rt)
        assert os.path.isfile(rt._state_marker_path())
        assert os.path.isfile(os.path.join(
            rt._data_dir(), "var-lib-libvirt-qemu/save/vm1.save"))

    def test_a_tree_renamed_before_the_crash_is_discarded(self, tmp_path):
        """The crash window runs from the first rename to the marker.

        A retry can find one tree already in place and still incomplete, so
        an absent marker has to invalidate everything at the destination,
        not just what is still staged.
        """
        rt = _runtime(tmp_path)
        data = rt._data_dir()
        stale = os.path.join(data, "etc-libvirt")
        os.makedirs(stale)
        with open(os.path.join(stale, "leftover.xml"), "w") as fobj:
            fobj.write("<stale/>")
        os.makedirs(os.path.join(
            data, "var-lib-libvirt-qemu" + rt._STAGING_SUFFIX))

        self._migrate(rt)

        assert not os.path.exists(os.path.join(stale, "leftover.xml"))
        assert os.path.isfile(os.path.join(stale, "libvirtd.conf"))
        assert os.path.isfile(rt._state_marker_path())

    def test_already_persisted_container_is_left_alone(self, tmp_path):
        rt = _runtime(tmp_path)
        calls = []
        self._migrate(rt, persisted=True, calls=calls)
        assert not any("docker cp" in c for c in calls)
        assert not os.path.exists(rt._state_host_dir("etc-libvirt"))

    def test_running_guests_warn_rather_than_refuse(self, tmp_path, caplog):
        """Migrating needs the container stopped, which stops the guests.

        Refusing here would break every unrelated read-only command for a
        user with a live cluster; scheduling that downtime is their call.
        """
        rt = _runtime(tmp_path)
        calls = []
        self._migrate(rt, guests=1, calls=calls)
        assert not any("docker cp" in c for c in calls)
        assert not os.path.isfile(rt._state_marker_path())

    def test_populated_destination_without_a_source_is_accepted(self,
                                                               tmp_path):
        """The normal state after any ``compose down``, not a failure.

        entrypoint.sh seeds both trees on a first run, so every instance
        whose container has since been removed has a populated destination
        and has never migrated anything. Refusing that would break a
        completely ordinary flow to catch a rare one.
        """
        rt = _runtime(tmp_path)
        os.makedirs(rt._state_host_dir("etc-libvirt"))
        with open(os.path.join(
                rt._state_host_dir("etc-libvirt"), "x.xml"), "w") as fobj:
            fobj.write("<domain/>")
        self._migrate(rt, state="")

    def test_interrupted_migration_without_a_source_refuses(self, tmp_path):
        """A staged tree is unambiguous evidence, where files alone are not."""
        rt = _runtime(tmp_path)
        os.makedirs(rt._state_host_dir("etc-libvirt"))
        os.makedirs(
            rt._state_host_dir("var-lib-libvirt-qemu") + rt._STAGING_SUFFIX)
        with pytest.raises(ProvisionError, match="half-finished migration"):
            self._migrate(rt, state="")

    def test_marked_destination_without_a_source_is_accepted(self, tmp_path):
        rt = _runtime(tmp_path)
        os.makedirs(rt._state_host_dir("etc-libvirt"))
        open(rt._state_marker_path(), "w").close()
        self._migrate(rt, state="")

    def test_live_mounts_write_the_marker(self, tmp_path):
        """Otherwise a later run cannot tell a seeded install from a
        half-finished migration once the container is gone."""
        rt = _runtime(tmp_path)
        self._migrate(rt, persisted=True)
        assert os.path.isfile(rt._state_marker_path())

    def test_unknown_container_existence_refuses(self, tmp_path):
        rt = _runtime(tmp_path)
        compose = str(tmp_compose(rt))
        with patch("invoke.run", return_value=MagicMock(ok=False, stdout="")):
            with pytest.raises(ProvisionError, match="cannot tell whether"):
                rt._ensure_state_persisted(compose, os.path.dirname(compose))


def tmp_compose(rt):
    """A real compose file inside the runtime dir, so it is boxman-owned."""
    runtime_dir = rt._get_local_runtime_dir()
    os.makedirs(runtime_dir, exist_ok=True)
    path = os.path.join(runtime_dir, "docker-compose.yml")
    with open(path, "w") as fobj:
        fobj.write("services: {}\n")
    return path


class TestStrandedDataDir:
    """#164 FB-11 — do not start against an empty relocated data dir."""

    def _rt_with_custom_compose(self, tmp_path):
        rt = _runtime(tmp_path)
        custom = tmp_path / "custom"
        custom.mkdir()
        return rt, str(custom)

    def test_populated_legacy_dir_refuses(self, tmp_path):
        rt, custom = self._rt_with_custom_compose(tmp_path)
        legacy = os.path.join(custom, "data", "images")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "disk.qcow2"), "w") as fobj:
            fobj.write("x")
        with pytest.raises(ProvisionError, match="still holds"):
            rt._assert_no_stranded_data_dir(custom)

    def test_the_message_names_both_directories(self, tmp_path):
        rt, custom = self._rt_with_custom_compose(tmp_path)
        os.makedirs(os.path.join(custom, "data", "ssh"))
        with open(os.path.join(custom, "data", "ssh", "id_ed25519"), "w") as f:
            f.write("key")
        with pytest.raises(ProvisionError) as excinfo:
            rt._assert_no_stranded_data_dir(custom)
        message = str(excinfo.value)
        assert os.path.join(custom, "data") in message
        assert rt._data_dir() in message

    def test_empty_legacy_dir_is_fine(self, tmp_path):
        rt, custom = self._rt_with_custom_compose(tmp_path)
        os.makedirs(os.path.join(custom, "data"))
        rt._assert_no_stranded_data_dir(custom)

    def test_absent_legacy_dir_is_fine(self, tmp_path):
        rt, custom = self._rt_with_custom_compose(tmp_path)
        rt._assert_no_stranded_data_dir(custom)

    def test_a_populated_new_dir_wins(self, tmp_path):
        rt, custom = self._rt_with_custom_compose(tmp_path)
        os.makedirs(os.path.join(custom, "data", "images"))
        with open(os.path.join(custom, "data", "images", "d"), "w") as fobj:
            fobj.write("x")
        os.makedirs(os.path.join(rt._data_dir(), "images"))
        with open(os.path.join(rt._data_dir(), "images", "d"), "w") as fobj:
            fobj.write("x")
        rt._assert_no_stranded_data_dir(custom)

    def test_boxman_owned_compose_never_trips(self, tmp_path):
        """For the bundled layout the two locations are the same directory."""
        rt = _runtime(tmp_path)
        runtime_dir = rt._get_local_runtime_dir()
        os.makedirs(os.path.join(runtime_dir, "data", "images"))
        with open(os.path.join(
                runtime_dir, "data", "images", "d"), "w") as fobj:
            fobj.write("x")
        rt._assert_no_stranded_data_dir(runtime_dir)


class TestDataDirIsUnified:
    """#164 FB-11 — .env and the compose environment must agree."""

    def test_env_file_and_compose_environment_agree(self, tmp_path):
        rt = _runtime(tmp_path)
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        compose = custom_dir / "docker-compose.yml"
        compose.write_text("services: {}\n")

        captured = {}
        started = {"yes": False}

        def _run(command, *_args, **kwargs):
            if "compose" in command and " up " in f" {command} ":
                captured["env"] = kwargs.get("env", {})
                started["yes"] = True
                return MagicMock(ok=True, stdout="")
            if "docker ps -a" in command:
                return MagicMock(ok=True, stdout="")
            if "docker inspect" in command and ".Mounts" in command:
                return MagicMock(ok=False, stdout="")
            if "docker inspect" in command:
                # not running until compose up, so ensure_ready takes the
                # start path and then finds the container up
                return MagicMock(
                    ok=True,
                    stdout="true\n" if started["yes"] else "false\n")
            return MagicMock(ok=True, stdout="")

        with patch.object(rt, "get_compose_file_path",
                          return_value=str(compose)), \
                patch.object(rt, "_log_compose_file"), \
                patch.object(rt, "verify_workdirs_accessible"), \
                patch("invoke.run", side_effect=_run):
            rt.ensure_ready()

        env_path = os.path.join(rt._get_local_runtime_dir(), ".env")
        written = dict(
            line.split("=", 1)
            for line in open(env_path).read().splitlines() if "=" in line)

        expected = rt._data_dir()
        assert written["BOXMAN_DATA_DIR"] == expected
        assert captured["env"]["BOXMAN_DATA_DIR"] == expected
        # the bug: the compose environment used the compose file's own dir
        assert captured["env"]["BOXMAN_DATA_DIR"] != str(
            custom_dir / "data")


class TestStartPathIsGuarded:
    """``up -d --build`` recreates on a compose-config change, so it is a
    container-destroying path too — and it is reached when
    ``_container_is_running()`` answers False without knowing, which it
    does for a docker failure exactly as for a stopped container."""

    def test_a_running_container_with_guests_is_not_rebuilt(self, tmp_path):
        rt = _runtime(tmp_path)
        compose = str(tmp_compose(rt))
        calls = []

        def _run(command, *_a, **_kw):
            calls.append(command)
            if "docker ps -a" in command:
                return MagicMock(ok=True, stdout="running\n")
            if rt._GUEST_PROBE_MARKER in command:
                return MagicMock(
                    ok=True, stdout=f"{rt._GUEST_PROBE_MARKER}1\n")
            if "docker inspect" in command and ".Mounts" in command:
                import json
                return MagicMock(ok=True, stdout=json.dumps(
                    [{"Source": rt._state_host_dir(sub),
                      "Destination": path, "RW": True}
                     for sub, path in rt._PERSISTED_STATE]))
            if "docker inspect" in command:
                # the conflated answer: a docker failure looks like this
                return MagicMock(ok=True, stdout="false\n")
            return MagicMock(ok=True, stdout="")

        with patch.object(rt, "get_compose_file_path", return_value=compose), \
                patch.object(rt, "_log_compose_file"), \
                patch("invoke.run", side_effect=_run):
            with pytest.raises(ProvisionError, match="1 QEMU guest"):
                rt.ensure_ready()

        assert not any(
            "compose" in c and " up " in f" {c} " for c in calls)

    def test_a_stopped_container_still_starts(self, tmp_path):
        rt = _runtime(tmp_path)
        compose = str(tmp_compose(rt))
        calls = []
        started = {"yes": False}

        def _run(command, *_a, **_kw):
            calls.append(command)
            if "compose" in command and " up " in f" {command} ":
                started["yes"] = True
                return MagicMock(ok=True, stdout="")
            if "docker ps -a" in command:
                return MagicMock(
                    ok=True,
                    stdout="running\n" if started["yes"] else "exited\n")
            if rt._GUEST_PROBE_MARKER in command:
                return MagicMock(
                    ok=True, stdout=f"{rt._GUEST_PROBE_MARKER}0\n")
            if "docker inspect" in command and ".Mounts" in command:
                import json
                return MagicMock(ok=True, stdout=json.dumps(
                    [{"Source": rt._state_host_dir(sub),
                      "Destination": path, "RW": True}
                     for sub, path in rt._PERSISTED_STATE]))
            if "docker inspect" in command:
                return MagicMock(
                    ok=True,
                    stdout="true\n" if started["yes"] else "false\n")
            return MagicMock(ok=True, stdout="")

        with patch.object(rt, "get_compose_file_path", return_value=compose), \
                patch.object(rt, "_log_compose_file"), \
                patch.object(rt, "verify_workdirs_accessible"), \
                patch("invoke.run", side_effect=_run):
            rt.ensure_ready()

        assert any("compose" in c and " up " in f" {c} " for c in calls)
