"""
Unit tests for PXE-related methods added to LibVirtSession:
  - set_boot_order
  - restore_boot_order
  - wait_for_ssh
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, call, patch

import pytest
from lxml import etree

from boxman.providers.libvirt.session import LibVirtSession


pytestmark = pytest.mark.unit


def _session(provider: dict | None = None) -> LibVirtSession:
    cfg = {"provider": {"libvirt": provider or {}}}
    return LibVirtSession(config=cfg)


def _result(stdout: str = "", ok: bool = True, stderr: str = "",
            return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


# ---------------------------------------------------------------------------
# set_boot_order / restore_boot_order
# ---------------------------------------------------------------------------

class TestSetBootOrder:

    def _make_domain_xml(self, boot_devs: list[str]) -> str:
        root = etree.Element('domain', type='kvm')
        os_elem = etree.SubElement(root, 'os')
        for dev in boot_devs:
            b = etree.SubElement(os_elem, 'boot')
            b.set('dev', dev)
        return etree.tostring(root, encoding='unicode', pretty_print=True)

    @patch('boxman.providers.libvirt.virsh_edit.VirshEdit.redefine_domain',
           return_value=True)
    @patch('boxman.providers.libvirt.virsh_edit.VirshEdit.get_domain_xml')
    def test_sets_boot_order(self, mock_get_xml, mock_redefine):
        xml = self._make_domain_xml(['hd'])
        mock_get_xml.return_value = xml

        session = _session()
        result = session.set_boot_order('myvm', ['network', 'hd'])

        assert result is True
        redefined_xml = mock_redefine.call_args[0][1]
        tree = etree.fromstring(redefined_xml.encode('utf-8'))
        boot_devs = [b.get('dev') for b in tree.findall('.//os/boot')]
        assert boot_devs == ['network', 'hd']

    @patch('boxman.providers.libvirt.virsh_edit.VirshEdit.redefine_domain',
           return_value=True)
    @patch('boxman.providers.libvirt.virsh_edit.VirshEdit.get_domain_xml')
    def test_replaces_existing_boot_entries(self, mock_get_xml, mock_redefine):
        xml = self._make_domain_xml(['network', 'hd', 'cdrom'])
        mock_get_xml.return_value = xml

        session = _session()
        session.set_boot_order('myvm', ['hd'])

        redefined_xml = mock_redefine.call_args[0][1]
        tree = etree.fromstring(redefined_xml.encode('utf-8'))
        boot_devs = [b.get('dev') for b in tree.findall('.//os/boot')]
        assert boot_devs == ['hd']

    @patch('boxman.providers.libvirt.virsh_edit.VirshEdit.redefine_domain',
           return_value=True)
    @patch('boxman.providers.libvirt.virsh_edit.VirshEdit.get_domain_xml')
    def test_creates_os_element_if_missing(self, mock_get_xml, mock_redefine):
        root = etree.Element('domain', type='kvm')
        xml = etree.tostring(root, encoding='unicode')
        mock_get_xml.return_value = xml

        session = _session()
        session.set_boot_order('myvm', ['network'])

        redefined_xml = mock_redefine.call_args[0][1]
        tree = etree.fromstring(redefined_xml.encode('utf-8'))
        os_elem = tree.find('os')
        assert os_elem is not None
        boot_devs = [b.get('dev') for b in os_elem.findall('boot')]
        assert boot_devs == ['network']


class TestRestoreBootOrder:

    @patch.object(LibVirtSession, 'set_boot_order', return_value=True)
    def test_delegates_to_set_boot_order_with_hd(self, mock_set):
        session = _session()
        result = session.restore_boot_order('myvm')
        assert result is True
        mock_set.assert_called_once_with('myvm', ['hd'])


# ---------------------------------------------------------------------------
# wait_for_ssh
# ---------------------------------------------------------------------------

class TestWaitForSsh:

    @patch('socket.create_connection')
    def test_returns_true_on_first_attempt(self, mock_conn):
        mock_conn.return_value.__enter__ = lambda s: s
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        session = _session()
        result = session.wait_for_ssh('192.168.1.10', timeout=30, interval=5)
        assert result is True

    @patch('time.sleep', return_value=None)
    @patch('socket.create_connection',
           side_effect=[OSError("refused"), OSError("refused"), MagicMock(
               __enter__=lambda s: s, __exit__=MagicMock(return_value=False))])
    def test_retries_until_success(self, mock_conn, mock_sleep):
        session = _session()
        result = session.wait_for_ssh('192.168.1.10', timeout=30, interval=5)
        assert result is True
        assert mock_sleep.call_count == 2

    @patch('time.sleep', return_value=None)
    @patch('socket.create_connection', side_effect=OSError("refused"))
    def test_returns_false_on_timeout(self, mock_conn, mock_sleep):
        session = _session()
        result = session.wait_for_ssh('192.168.1.10', timeout=10, interval=5)
        assert result is False

    @patch('socket.create_connection')
    def test_uses_specified_port(self, mock_conn):
        mock_conn.return_value.__enter__ = lambda s: s
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        session = _session()
        session.wait_for_ssh('192.168.1.10', port=2222, timeout=30)
        mock_conn.assert_called_once_with(('192.168.1.10', 2222), timeout=5)
