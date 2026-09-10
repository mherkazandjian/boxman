"""
``boxman update`` no longer power-cycles a running guest unasked (#164 C1).

Any change libvirt cannot apply to a live domain -- raising a vCPU or
memory ceiling, a shared-folder change it will not hot-plug, a memballoon
edit -- used to make ``update`` shut the guest down and start it again,
with no flag, no prompt and no mention in the help text. Someone changing
a shared folder on a fleet lost every running VM on it for the duration.

The change is still written to the persistent config; only the restart is
deferred, and ``--restart`` opts back in. ``--yes`` deliberately does not:
it answers the VM-removal prompt, and using it to run update unattended is
not agreement to reboot a guest.
"""

from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.vm_differ import VMStateDiffer
from conftest import make_bare_manager

pytestmark = pytest.mark.unit


def _diff(**overrides):
    """A diff with nothing changed; override the parts a test cares about."""
    base = {
        'cpu_changed': False,
        'memory_changed': False,
        'max_vcpus_changed': False,
        'max_memory_changed': False,
        'new_disks': [],
        'resize_disks': [],
        'removed_disks': [],
        'refused_disk_removals': [],
        'unowned_disks': [],
        'disk_conflicts': [],
        'shared_folders_restart_pending': False,
        'new_cdroms': [],
        'removed_cdroms': [],
        'changed_cdroms': [],
        'new_shared_folders': [],
        'removed_shared_folders': [],
        'changed_shared_folders': [],
        'memballoon_changed': False,
        'memballoon_restart_pending': False,
        'actual_cpus': 2,
        'desired_cpus': 2,
        'actual_memory_mb': 2048,
        'desired_memory_mb': 2048,
        'desired_max_vcpus': None,
        'desired_max_memory_mb': None,
        'vm_state': 'running',
    }
    base.update(overrides)
    return base


def _run_worker(diff, vm_info, allow_restart=False, **provider_returns):
    """Drive ``_update_single_vm`` over *diff*; return (mgr, result)."""
    mgr = make_bare_manager({'project': 'demo'})
    mgr.provider = MagicMock()
    mgr.provider.provider_config = {'uri': 'qemu:///system'}
    mgr.provider.update_vm_cpu_memory.return_value = provider_returns.get(
        'cpu_memory', {'success': True, 'restart_needed': False})
    mgr.provider.update_vm_shared_folders.return_value = provider_returns.get(
        'shared_folders', {'success': True, 'restart_needed': False})
    mgr.provider.configure_vm_memballoon.return_value = True
    mgr.provider.update_vm_disks.return_value = True
    mgr.provider.update_vm_cdroms.return_value = True
    mgr.provider.shutdown_and_wait.return_value = True
    mgr.provider.start_vm.return_value = True
    result_queue = MagicMock()

    with patch.object(VMStateDiffer, 'diff_vm', return_value=diff):
        mgr._update_single_vm(
            'cluster1', {'workdir': '/tmp'}, 'node01', vm_info,
            result_queue, dry_run=False, allow_restart=allow_restart)

    return mgr, result_queue.put.call_args.args[0][1]


class TestRestartIsDeferredByDefault:
    """Each source of a restart, deferred rather than acted on."""

    def test_cpu_ceiling_change_defers(self):
        mgr, result = _run_worker(
            _diff(cpu_changed=True, desired_cpus=4),
            {'cpus': 4},
            cpu_memory={'success': True, 'restart_needed': True})

        assert result['status'] == 'needs_restart'
        mgr.provider.shutdown_and_wait.assert_not_called()
        mgr.provider.start_vm.assert_not_called()

    def test_shared_folder_change_defers(self):
        mgr, result = _run_worker(
            _diff(new_shared_folders=[{'name': 'share', 'source': '/srv'}]),
            {'shared_folders': [{'name': 'share', 'source': '/srv'}]},
            shared_folders={'success': True, 'restart_needed': True})

        assert result['status'] == 'needs_restart'
        mgr.provider.shutdown_and_wait.assert_not_called()

    def test_memballoon_change_defers(self):
        mgr, result = _run_worker(
            _diff(memballoon_changed=True, memballoon_restart_pending=True,
                  desired_memballoon={'autodeflate': True},
                  actual_memballoon={'autodeflate': False},
                  live_memballoon={'autodeflate': False}),
            {'memballoon': {'autodeflate': True}})

        assert result['status'] == 'needs_restart'
        mgr.provider.shutdown_and_wait.assert_not_called()

    def test_the_change_is_still_applied(self):
        """Deferred, not skipped -- the persistent config is written."""
        mgr, result = _run_worker(
            _diff(cpu_changed=True, desired_cpus=4),
            {'cpus': 4},
            cpu_memory={'success': True, 'restart_needed': True})

        mgr.provider.update_vm_cpu_memory.assert_called_once()
        assert 'persistent config' in result['details']

    def test_the_message_names_the_way_out(self):
        _mgr, result = _run_worker(
            _diff(cpu_changed=True, desired_cpus=4),
            {'cpus': 4},
            cpu_memory={'success': True, 'restart_needed': True})

        assert '--restart' in result['details']


class TestRestartOptIn:

    def test_restart_flag_authorises_the_power_cycle(self):
        mgr, result = _run_worker(
            _diff(cpu_changed=True, desired_cpus=4),
            {'cpus': 4}, allow_restart=True,
            cpu_memory={'success': True, 'restart_needed': True})

        assert result['status'] == 'updated'
        assert '(restarted)' in result['details']
        mgr.provider.shutdown_and_wait.assert_called_once()
        mgr.provider.start_vm.assert_called_once()

    def test_yes_alone_does_not_authorise_a_restart(self):
        """``--yes`` answers the removal prompt, nothing more.

        Driven through ``update()`` rather than the worker, because the
        question is what the flag is read as at the top of the run.
        """
        import types

        from boxman.manager import BoxmanManager

        full = 'bprj__demo__bprj_cluster_1_node01'
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            'project': 'demo',
            'clusters': {'cluster_1': {'vms': {'node01': {'cpus': 4}}}},
        }
        mgr.logger = MagicMock()
        captured = {}

        def _capture(tasks, op_label='parallel task'):
            for _label, _target, args in tasks:
                captured['allow_restart'] = args[-1]
            return {}, {}

        with patch.multiple(
                BoxmanManager,
                _update_sessions_with_runtime=lambda self: None,
                ensure_shared_bridges=lambda self: None,
                reconcile_networks=lambda self, **kw: {},
                report_network_results=lambda self, r: None,
                raise_on_network_failures=lambda self, r: None,
                _find_all_existing_project_vms=lambda self: [full],
                _normalize_cdroms_for_update=lambda self, names, allow_fetch=True: {},
                setup_ssh_access=lambda self: None,
                connect_info=lambda self: None,
                _run_parallel=lambda self, tasks, op_label='x', max_workers=None:
                    _capture(tasks, op_label)):
            mgr.update(types.SimpleNamespace(
                dry_run=False, yes=True, recreate_networks=False))

        assert captured.get('allow_restart') is False, (
            '--yes must not authorise power-cycling a running guest')

    def test_restart_flag_reaches_the_worker(self):
        import types

        from boxman.manager import BoxmanManager

        full = 'bprj__demo__bprj_cluster_1_node01'
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            'project': 'demo',
            'clusters': {'cluster_1': {'vms': {'node01': {'cpus': 4}}}},
        }
        mgr.logger = MagicMock()
        captured = {}

        def _capture(tasks, op_label='parallel task'):
            for _label, _target, args in tasks:
                captured['allow_restart'] = args[-1]
            return {}, {}

        with patch.multiple(
                BoxmanManager,
                _update_sessions_with_runtime=lambda self: None,
                ensure_shared_bridges=lambda self: None,
                reconcile_networks=lambda self, **kw: {},
                report_network_results=lambda self, r: None,
                raise_on_network_failures=lambda self, r: None,
                _find_all_existing_project_vms=lambda self: [full],
                _normalize_cdroms_for_update=lambda self, names, allow_fetch=True: {},
                setup_ssh_access=lambda self: None,
                connect_info=lambda self: None,
                _run_parallel=lambda self, tasks, op_label='x', max_workers=None:
                    _capture(tasks, op_label)):
            mgr.update(types.SimpleNamespace(
                dry_run=False, yes=False, restart=True,
                recreate_networks=False))

        assert captured.get('allow_restart') is True


class TestPausedGuest:
    """A paused guest is active but cannot be cleanly restarted."""

    def test_paused_guest_is_reported_not_restarted(self):
        mgr, result = _run_worker(
            _diff(vm_state='paused', cpu_changed=True, desired_cpus=4),
            {'cpus': 4},
            cpu_memory={'success': True, 'restart_needed': True})

        assert result['status'] == 'needs_restart'
        mgr.provider.shutdown_and_wait.assert_not_called()

    def test_paused_guest_not_restarted_even_with_the_flag(self):
        mgr, _result = _run_worker(
            _diff(vm_state='paused', cpu_changed=True, desired_cpus=4),
            {'cpus': 4}, allow_restart=True,
            cpu_memory={'success': True, 'restart_needed': True})

        mgr.provider.shutdown_and_wait.assert_not_called()


class TestCeilingOnlyChangeNeedsARestart:
    """A raised ceiling with no change to the current size (#164 C1).

    ``update_vm_cpu_memory`` decided ``restart_needed`` from which of its
    two live-change blocks ran. Both are gated on the *current* cpu count
    or memory size changing, and a ceiling-only change leaves ``cpus`` and
    ``memory_mb`` None -- so neither ran, nothing set the flag, and the
    call returned ``restart_needed: False``.

    The persistent config had already been rewritten by that point, and
    libvirt cannot raise a live ceiling, so the guest kept the old one
    while ``update`` reported a plain "updated".
    """

    def _session(self):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        return session

    def _editor(self):
        editor = MagicMock()
        editor.get_domain_xml.return_value = "<domain/>"
        editor.cpu_memory_modifications.return_value = ([('xpath', 'v')], False)
        editor.modify_xml_xpath.return_value = "<domain/>"
        editor.redefine_domain.return_value = True
        return editor

    def test_max_vcpus_only_needs_a_restart(self):
        session = self._session()
        with patch('boxman.providers.libvirt.session.VirshEdit',
                   return_value=self._editor()):
            result = session.update_vm_cpu_memory(
                vm_name='node01',
                cpus=None,
                memory_mb=None,
                vm_state='running',
                actual_cpus={'total_vcpus': 4},
                actual_memory_mb=2048,
                max_vcpus=8,
                max_memory_mb=None)

        assert result['success'] is True
        assert result['restart_needed'] is True

    def test_max_memory_only_needs_a_restart(self):
        session = self._session()
        differ = MagicMock()
        differ.get_max_memory_mb.return_value = 2048
        with patch('boxman.providers.libvirt.session.VirshEdit',
                   return_value=self._editor()), \
             patch('boxman.providers.libvirt.vm_differ.VMStateDiffer',
                   return_value=differ):
            result = session.update_vm_cpu_memory(
                vm_name='node01',
                cpus=None,
                memory_mb=None,
                vm_state='running',
                actual_cpus={'total_vcpus': 4},
                actual_memory_mb=2048,
                max_vcpus=None,
                max_memory_mb=8192)

        assert result['restart_needed'] is True

    def test_ceiling_already_at_the_desired_value_needs_nothing(self):
        """Self-correcting: no restart when the live ceiling already matches."""
        session = self._session()
        with patch('boxman.providers.libvirt.session.VirshEdit',
                   return_value=self._editor()):
            result = session.update_vm_cpu_memory(
                vm_name='node01',
                cpus=None,
                memory_mb=None,
                vm_state='running',
                actual_cpus={'total_vcpus': 8},
                actual_memory_mb=2048,
                max_vcpus=8,
                max_memory_mb=None)

        assert result['restart_needed'] is False

    def test_a_stopped_vm_never_needs_a_restart(self):
        session = self._session()
        with patch('boxman.providers.libvirt.session.VirshEdit',
                   return_value=self._editor()):
            result = session.update_vm_cpu_memory(
                vm_name='node01',
                cpus=None,
                memory_mb=None,
                vm_state='shut off',
                actual_cpus={'total_vcpus': 4},
                actual_memory_mb=2048,
                max_vcpus=8,
                max_memory_mb=None)

        assert result['restart_needed'] is False


class TestPausedGuestAtTheProvider:
    """The provider half of the paused contract (#164 C1 review, finding 6).

    ``TestPausedGuest`` drives the worker with a mocked provider result of
    ``restart_needed: True`` -- which the real provider never produced for
    a paused guest, because every non-running state took the cold path and
    returned False. So the worker test passed while the actual behaviour
    was: persistent config written, guest untouched, run reported clean.
    """

    def _session(self):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        return session

    def _editor(self):
        editor = MagicMock()
        editor.get_domain_xml.return_value = "<domain/>"
        editor.cpu_memory_modifications.return_value = ([('x', 'v')], False)
        editor.modify_xml_xpath.return_value = "<domain/>"
        editor.redefine_domain.return_value = True
        editor.configure_cpu_memory.return_value = True
        return editor

    def _call(self, vm_state, **kwargs):
        session = self._session()
        params = dict(
            vm_name='node01', cpus=None, memory_mb=None, vm_state=vm_state,
            actual_cpus={'total_vcpus': 2, 'current_vcpus': 2},
            actual_memory_mb=2048, max_vcpus=None, max_memory_mb=None)
        params.update(kwargs)
        with patch('boxman.providers.libvirt.session.VirshEdit',
                   return_value=self._editor()):
            return session.update_vm_cpu_memory(**params)

    def test_a_paused_cpu_change_reports_pending(self):
        result = self._call('paused', cpus={'sockets': 1, 'cores': 4,
                                            'threads': 1})

        assert result['success'] is True
        assert result['restart_needed'] is True

    def test_a_paused_memory_change_reports_pending(self):
        result = self._call('paused', memory_mb=4096)

        assert result['restart_needed'] is True

    def test_a_paused_ceiling_only_change_reports_pending(self):
        result = self._call('paused', max_vcpus=8)

        assert result['restart_needed'] is True

    def test_a_paused_no_op_reports_nothing_pending(self):
        """Self-correcting: matching live state needs no restart."""
        result = self._call('paused', max_vcpus=2)

        assert result['restart_needed'] is False

    @pytest.mark.parametrize("state", ["shut off", "shutoff"])
    def test_a_genuinely_stopped_guest_needs_no_restart(self, state):
        """The next boot uses the new config; nothing is pending."""
        result = self._call(state, cpus={'sockets': 1, 'cores': 4,
                                         'threads': 1}, max_vcpus=8)

        assert result['restart_needed'] is False
        assert result['method'] == 'cold'


class TestPausedTopologyOnlyChange:
    """Same vCPU count, different shape (#164 C1 review round 2, finding 6).

    Two sockets of one core to one socket of two cores keeps the total.
    diff_vm() sees the change and the persistent XML is rewritten, but
    comparing totals alone reported nothing pending, so the worker called
    the run updated while the paused guest kept the old topology.
    """

    def _call(self, **kwargs):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        editor = MagicMock()
        editor.get_domain_xml.return_value = "<domain/>"
        editor.cpu_memory_modifications.return_value = ([('x', 'v')], False)
        editor.modify_xml_xpath.return_value = "<domain/>"
        editor.redefine_domain.return_value = True
        editor.configure_cpu_memory.return_value = True
        params = dict(
            vm_name='node01', cpus=None, memory_mb=None, vm_state='paused',
            actual_cpus={'sockets': 2, 'cores': 1, 'threads': 1,
                         'total_vcpus': 2, 'current_vcpus': 2},
            actual_memory_mb=2048, max_vcpus=None, max_memory_mb=None)
        params.update(kwargs)
        with patch('boxman.providers.libvirt.session.VirshEdit',
                   return_value=editor):
            return session.update_vm_cpu_memory(**params)

    def test_a_reshaped_topology_is_pending(self):
        result = self._call(cpus={'sockets': 1, 'cores': 2, 'threads': 1})

        assert result['restart_needed'] is True

    def test_an_identical_topology_is_not(self):
        result = self._call(cpus={'sockets': 2, 'cores': 1, 'threads': 1})

        assert result['restart_needed'] is False
