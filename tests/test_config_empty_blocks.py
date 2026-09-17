"""
Regression tests for a config block that rendered empty.

A box whose ``vms:`` block is filled by a Jinja loop renders it with nothing
under it when the loop matches nothing — the two-host Proxmox box does this
for the site that owns no node when ``BOXMAN_SITE`` is unset. YAML parses
such a block as ``null``, and readers written for a mapping crashed on it:
``boxman ps`` raised ``AttributeError: 'NoneType' object has no attribute
'keys'`` instead of reporting that nothing is defined.

The loader now hands every downstream reader the empty mapping it expects,
for ``clusters:``, ``vms:`` and ``boxes:`` alike. A block that is *absent*
stays absent — only a present-but-empty one is normalised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager
from boxman.scripts.app import main

pytestmark = pytest.mark.unit


def _manager() -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.logger = MagicMock()
    mgr.config_path = None
    mgr.rendered_config_text = None
    return mgr


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "conf.yml"
    path.write_text(text)
    return str(path)


# The shape that produced the traceback: the block is present, and the loop
# that fills it selects nothing for this site.
SITE_FILTERED_VMS = """
{% set site = env("BOXMAN_SITE") %}
{% set nodes = [{'name': 'pve1', 'site': 'host1'}] %}
project: demo
clusters:
  cluster_1:
    workdir: /tmp/ws/c1
    vms:
{% for n in nodes if n.site == site %}
      {{ n.name }}:
        hostname: {{ n.name }}
{% endfor %}
"""


class TestEmptyBlocksAreEmptyMappings:

    def test_vms_that_rendered_empty_is_an_empty_mapping(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.delenv("BOXMAN_SITE", raising=False)
        conf = _manager().load_config(_write(tmp_path, SITE_FILTERED_VMS))

        assert conf['clusters']['cluster_1']['vms'] == {}

    def test_the_same_template_still_renders_the_matching_site(self, tmp_path,
                                                                monkeypatch):
        monkeypatch.setenv("BOXMAN_SITE", "host1")
        conf = _manager().load_config(_write(tmp_path, SITE_FILTERED_VMS))

        assert conf['clusters']['cluster_1']['vms'] == {
            'pve1': {'hostname': 'pve1'},
        }

    def test_a_literal_null_vms_block_is_an_empty_mapping(self, tmp_path):
        conf = _manager().load_config(_write(tmp_path, """
project: demo
clusters:
  cluster_1:
    workdir: /tmp/ws/c1
    vms:
"""))

        assert conf['clusters']['cluster_1']['vms'] == {}

    def test_boxes_that_rendered_empty_under_v2_is_an_empty_vms_mapping(
            self, tmp_path):
        conf = _manager().load_config(_write(tmp_path, """
version: '2.0'
project: demo
clusters:
  cluster_1:
    workdir: /tmp/ws/c1
    boxes:
"""))

        cluster = conf['clusters']['cluster_1']
        assert cluster['vms'] == {}
        assert 'boxes' not in cluster

    def test_clusters_that_rendered_empty_is_an_empty_mapping(self, tmp_path):
        conf = _manager().load_config(_write(tmp_path, """
project: demo
clusters:
"""))

        assert conf['clusters'] == {}

    def test_an_absent_block_stays_absent(self, tmp_path):
        conf = _manager().load_config(_write(tmp_path, """
project: demo
clusters:
  cluster_1:
    workdir: /tmp/ws/c1
"""))

        assert 'vms' not in conf['clusters']['cluster_1']
        assert 'boxes' not in conf['clusters']['cluster_1']


APP_CONFIG = {
    'runtime': 'local',
    'providers': {'libvirt': {'uri': 'qemu:///system'}},
}


def _run_cli(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["boxman"] + argv):
        try:
            main()
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0


def _run_ps(conf_path: str, *extra: str) -> int:
    """Run ``boxman ps`` against *conf_path* with libvirt and the cache
    stubbed out; the command must not reach virsh for an empty site."""
    virsh = MagicMock()
    with patch("boxman.manager.BoxmanCache"), \
         patch("boxman.scripts.app.load_boxman_config",
               return_value=APP_CONFIG), \
         patch("boxman.manager.VirshCommand", return_value=virsh):
        code = _run_cli(["--conf", conf_path, "ps", *extra])
    assert not virsh.execute.called
    return code


class TestPsOnAnEmptySite:

    def test_ps_reports_nothing_defined_and_exits_zero(self, tmp_path,
                                                        monkeypatch, capsys):
        monkeypatch.delenv("BOXMAN_SITE", raising=False)
        conf_path = _write(tmp_path, SITE_FILTERED_VMS)

        code = _run_ps(conf_path)

        assert code == 0
        assert "No VMs or containers defined" in capsys.readouterr().out

    def test_ps_json_is_an_empty_list(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("BOXMAN_SITE", raising=False)
        conf_path = _write(tmp_path, SITE_FILTERED_VMS)

        code = _run_ps(conf_path, "--json")

        assert code == 0
        assert json.loads(capsys.readouterr().out) == []
