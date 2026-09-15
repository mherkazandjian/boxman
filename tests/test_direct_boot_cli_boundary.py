"""
The CLI boundary for direct-boot network validation (#171 C3).

The other tests for this call ``provision``/``update`` directly, and the
pre-existing exit-code tests stub the validator out entirely -- so they prove
the ``BoxmanError`` -> exit 2 translation, or they prove the validation, but
never both at once. These drive a real config through ``main()`` and assert the
exit code *and* that nothing was built.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import patch

import pytest

from boxman.exceptions import ConfigError
from boxman.scripts.app import main

pytestmark = pytest.mark.unit

_CONF = """
project: demo
clusters:
  cluster_1:
    workdir: {workdir}
    vms:
      node01:
        boot_order: [cdrom, hd]
        cdroms:
          - source: /nonexistent/installer.iso
        networks:
          - name: pvenet
            mac: "{mac}"
"""


class _Recorder(logging.Handler):
    """boxman's logger does not propagate, so pytest's caplog never sees it and
    its own handler predates capsys/capfd. Attaching here is the only way to
    read the one line main() prints for a BoxmanError."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _run(argv: list[str]) -> int:
    """Invoke main() with *argv*; return the exit code.

    NOTE: `--conf` is a *global* flag and must precede the subcommand. With it
    after, argparse rejects the command line with its own exit 2 -- which is
    indistinguishable from boxman's typed-error exit 2, and made an earlier
    version of these tests pass without ever reaching the validation.
    """
    recorder = _Recorder()
    logger = logging.getLogger("boxman")
    logger.addHandler(recorder)
    try:
        with patch.object(sys, "argv", ["boxman"] + argv):
            try:
                main()
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 1
        return 0
    finally:
        logger.removeHandler(recorder)
        _run.last_messages = recorder.messages


def _conf(tmp_path, mac: str) -> str:
    path = tmp_path / "conf.yml"
    path.write_text(_CONF.format(workdir=tmp_path / "ws", mac=mac))
    return str(path)


@pytest.mark.parametrize("mac", ["not-a-mac", "52:54:00:0c:01", "52-54-00-0c-01-01"])
def test_a_malformed_mac_exits_2_and_builds_nothing(tmp_path, mac):
    conf = _conf(tmp_path, mac)
    with patch("boxman.manager.BoxmanManager._update_sessions_with_runtime") as reached, \
         patch("boxman.manager_parts.images.ImagesMixin.ensure_templates_exist") as templates, \
         patch("boxman.manager_parts.vms.VMsMixin.clone_vms") as clone, \
         patch("boxman.manager_parts.networks.NetworksMixin.define_networks") as networks:
        code = _run(["--conf", conf, "provision"])

    assert code == 2, f"expected a clean exit 2, got {code}"
    # argparse exits 2 for a usage error too, so the code alone proves
    # nothing -- an earlier version of this test passed with `--conf` after the
    # subcommand, never reaching boxman at all. `provision` running is what
    # separates the two.
    reached.assert_called()
    # ...and that the exit came from *this* validation, not some later failure
    assert any("mac" in m.lower() for m in _run.last_messages), _run.last_messages

    templates.assert_not_called()
    clone.assert_not_called()
    networks.assert_not_called()


def test_a_blank_network_entry_exits_2(tmp_path):
    """#171 A4 at the boundary: it used to land the VM on libvirt's default."""
    path = tmp_path / "conf.yml"
    path.write_text(_CONF.format(workdir=tmp_path / "ws", mac="52:54:00:0c:01:01")
                    .replace('- name: pvenet\n            mac: "52:54:00:0c:01:01"', '- ""'))
    with patch("boxman.manager.BoxmanManager._update_sessions_with_runtime") as reached, \
         patch("boxman.manager_parts.images.ImagesMixin.ensure_templates_exist") as templates, \
         patch("boxman.manager_parts.vms.VMsMixin.clone_vms") as clone:
        code = _run(["--conf", str(path), "provision"])

    assert code == 2, f"expected a clean exit 2, got {code}"
    reached.assert_called()
    assert any("blank" in m.lower() for m in _run.last_messages), _run.last_messages

    templates.assert_not_called()
    clone.assert_not_called()


def test_the_boundary_really_routes_through_the_validation(tmp_path):
    """Without this the tests above could pass on any refusal at all.

    The validator is replaced with one that raises a recognisable ConfigError,
    proving both that `provision` reaches it and that main() translates a
    typed error into exit 2 -- and stopping there, so nothing downstream runs.
    """
    conf = _conf(tmp_path, "52:54:00:0c:01:01")
    with patch("boxman.manager_parts.images.ImagesMixin.validate_direct_boot_config",
               side_effect=ConfigError("sentinel-from-the-preflight")) as v:
        code = _run(["--conf", conf, "provision"])

    v.assert_called()
    assert code == 2
    assert any("sentinel-from-the-preflight" in m for m in _run.last_messages), \
        _run.last_messages

