"""
Regression tests for #164 X6 and CL-C1 — the config load path.

X6: malformed project or app YAML escaped the typed-error boundary in
``app.main()``. PyYAML and Jinja2 raise their own exception types, so a
missing colon printed a multi-line traceback and exited 1 instead of one
line and exit 2.

CL-C1: the rendered-config dump was written unconditionally, so a
read-only config dir or a full disk killed every command that loads a
config — ``ps``, ``conf`` and ``list`` included — with a PermissionError
traceback. The dump is a debugging aid and must never fail the command.
"""

import os
import stat
from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ConfigError
from boxman.manager import BoxmanManager
from boxman.scripts.app import load_boxman_config

pytestmark = pytest.mark.unit


def _manager():
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.logger = MagicMock()
    mgr.config_path = None
    mgr.rendered_config_text = None
    return mgr


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


VALID = """
project: demo
clusters:
  cluster_1:
    workdir: /tmp/ws/c1
    vms:
      node01: {}
"""


class TestProjectConfigErrors:

    def test_malformed_yaml_is_a_config_error(self, tmp_path):
        path = _write(tmp_path, "conf.yml", "project: demo\n  bad: indent\n")
        with pytest.raises(ConfigError) as excinfo:
            _manager().load_config(path)

        message = str(excinfo.value)
        assert "invalid YAML" in message
        assert "conf.yml" in message
        # One line, so the CLI's BoxmanError → exit 2 mapping can print it
        # as the whole user interface.
        assert "\n" not in message

    def test_yaml_error_names_the_position(self, tmp_path):
        path = _write(tmp_path, "conf.yml", "project: demo\n  bad: indent\n")
        with pytest.raises(ConfigError, match=r"line \d+, column \d+"):
            _manager().load_config(path)

    def test_broken_jinja_is_a_config_error(self, tmp_path):
        path = _write(tmp_path, "conf.yml",
                      "project: {% if %}demo{% endif %}\n")
        with pytest.raises(ConfigError) as excinfo:
            _manager().load_config(path)

        message = str(excinfo.value)
        assert "could not render" in message
        assert "\n" not in message

    def test_env_required_error_is_not_reworded(self, tmp_path):
        """env_required() raises a ConfigError naming the variable; the
        generic template wrapper must not bury it."""
        path = _write(tmp_path, "conf.yml",
                      'project: {{ env_required("BOXMAN_NOT_SET_XYZ") }}\n')
        os.environ.pop("BOXMAN_NOT_SET_XYZ", None)
        with pytest.raises(ConfigError, match="BOXMAN_NOT_SET_XYZ"):
            _manager().load_config(path)

    def test_valid_config_still_loads(self, tmp_path):
        path = _write(tmp_path, "conf.yml", VALID)
        conf = _manager().load_config(path)
        assert conf["project"] == "demo"


class TestAppConfigErrors:

    def test_malformed_app_yaml_is_a_config_error(self, tmp_path):
        path = _write(tmp_path, "boxman.yml", "provider: libvirt\n  bad: indent\n")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_boxman_config(path)

    def test_broken_app_jinja_is_a_config_error(self, tmp_path):
        path = _write(tmp_path, "boxman.yml", "provider: {% if %}x{% endif %}\n")
        with pytest.raises(ConfigError, match="could not render"):
            load_boxman_config(path)


# root ignores the mode bits, so the read-only cases would pass without
# ever exercising the failure they exist for.
not_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write to a read-only directory")


@not_root
class TestRenderedDumpIsBestEffort:

    def test_read_only_config_dir_does_not_fail_the_load(self, tmp_path):
        """CL-C1: `ps`, `conf` and `list` all load the config. A config dir
        the process cannot write to must not stop them."""
        path = _write(tmp_path, "conf.yml", VALID)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
        try:
            mgr = _manager()
            conf = mgr.load_config(path)
        finally:
            os.chmod(tmp_path, stat.S_IRWXU)

        assert conf["project"] == "demo"
        assert not os.path.exists(tmp_path / "conf.rendered.yml")
        mgr.logger.warning.assert_called_once()
        assert "rendered config" in mgr.logger.warning.call_args[0][0]

    def test_rendered_text_is_kept_in_memory(self, tmp_path):
        path = _write(tmp_path, "conf.yml", VALID)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
        try:
            mgr = _manager()
            mgr.load_config(path)
        finally:
            os.chmod(tmp_path, stat.S_IRWXU)

        assert "project: demo" in mgr.rendered_config_text

    def test_writable_dir_still_writes_the_dump(self, tmp_path):
        path = _write(tmp_path, "conf.yml", VALID)
        mgr = _manager()
        mgr.load_config(path)
        assert (tmp_path / "conf.rendered.yml").is_file()
        mgr.logger.warning.assert_not_called()


class TestShowConfPrefersMemory:

    def test_conf_reports_the_rendered_text_without_a_dump(self, tmp_path, capsys):
        """`boxman conf` used to read the dump back off disk, so it printed
        'not found' exactly when the dump could not be written."""
        mgr = _manager()
        mgr.config_path = str(tmp_path / "conf.yml")
        mgr.rendered_config_text = VALID

        mgr.show_conf(MagicMock(json=False), merged_provider={})
        out = capsys.readouterr().out

        assert "project: demo" in out
        assert "no rendered config available" not in out

    def test_conf_says_so_when_there_is_nothing_to_show(self, tmp_path, capsys):
        mgr = _manager()
        mgr.config_path = str(tmp_path / "conf.yml")

        mgr.show_conf(MagicMock(json=False), merged_provider={})
        out = capsys.readouterr().out

        assert "no rendered config available" in out
