"""
#276 — ``down`` on a VM that is already down.

``down`` handed every VM to the session's save (with ``--suspend``, its
suspend), and both refuse a VM that is not running. A VM that was shut off,
or saved by an earlier ``down``, failed the run with rc 2 and was reported
as being in an undefined state, when its state was plain to see.

These drive the real ``down`` with libvirt's states as ``_get_vm_states``
reports them, and a session that refuses what the libvirt one refuses.
"""

import types

import pytest

from boxman.exceptions import ProvisionError
from conftest import make_bare_manager

pytestmark = pytest.mark.unit

PRJ = 'bprj__demo__bprj'
VM1 = f'{PRJ}_c1_node01'
VM2 = f'{PRJ}_c1_node02'
WORKDIR = '/tmp/ws/c1'


class _Session:
    """
    The libvirt session's save and suspend, as far as ``down`` sees them:
    each records the VM it was asked about and returns False for one it
    cannot act on. Save takes a running or a paused guest, suspend only a
    running one.
    """

    def __init__(self, states, save_ok=True):
        self.states = states
        self.save_ok = save_ok
        self.saved = []
        self.suspended = []

    def save_vm(self, vm_name, workdir):
        self.saved.append(vm_name)
        return (self.save_ok
                and self.states.get(vm_name) in ('running', 'paused'))

    def suspend_vm(self, vm_name):
        self.suspended.append(vm_name)
        return self.states.get(vm_name) == 'running'


def _run_in_process(tasks, op_label='parallel task', max_workers=None):
    """_run_parallel's contract, with the workers run here."""
    results, failures = {}, {}
    for label, target, args in tasks:
        try:
            results[label] = target(*args)
        except Exception as exc:                           # noqa: BLE001
            failures[label] = f"{type(exc).__name__}: {exc}"
    return results, failures


def _manager(states, vms=(VM1,), save_ok=True, states_error=None):
    """
    A manager whose ``down`` sees *states* for *vms*.

    *states* is both what libvirt reports and what the session acts on;
    *states_error*, when given, is what reading the states raises instead.
    """
    mgr = make_bare_manager({
        'project': 'demo',
        'clusters': {'c1': {'workdir': WORKDIR,
                            'vms': {'node01': {}, 'node02': {}}}},
    })
    session = _Session(states, save_ok=save_ok)

    def _get_vm_states():
        if states_error is not None:
            raise states_error
        return dict(states)

    mgr._update_sessions_with_runtime = lambda: None
    mgr._control_vm_targets = lambda cli_args: [(vm, WORKDIR) for vm in vms]
    mgr._get_vm_states = _get_vm_states
    mgr.session_for_vm = lambda vm_name: session
    mgr._run_parallel = _run_in_process
    mgr.stop_compose_clusters = lambda: None
    return mgr, session


def _down(mgr, suspend=False):
    mgr.down(types.SimpleNamespace(suspend=suspend, vms='all', cluster=None))


def _info(mgr):
    return [c.args[0] for c in mgr.logger.info.call_args_list]


class TestDownLeavesAVmAlreadyDownAsItIs:

    @pytest.mark.parametrize('state', ['shut off', 'shutoff'])
    def test_a_shut_off_vm_is_left_as_it_is_and_the_run_succeeds(self, state):
        mgr, session = _manager({VM1: state})

        _down(mgr)

        assert session.saved == []
        assert f"VM '{VM1}' is already shut off; leaving it as it is" \
            in _info(mgr)
        assert 'infrastructure is down' in _info(mgr)

    def test_a_vm_saved_by_an_earlier_down_is_left_as_it_is(self):
        """
        Running ``down`` twice: the second finds the managed save the first
        wrote, which libvirt reports as a plain shut-off domain.
        """
        mgr, session = _manager({VM1: 'managedsave'})

        _down(mgr)

        assert session.saved == []
        assert f"VM '{VM1}' is already saved; leaving it as it is" \
            in _info(mgr)

    def test_a_paused_vm_is_saved_and_said_to_come_back_running(self):
        """
        A paused guest still holds its memory on the host, so it is not
        down yet. libvirt can save it, and saves it to come back running.
        """
        mgr, session = _manager({VM1: 'paused'})

        _down(mgr)

        assert session.saved == [VM1]
        assert (f"VM '{VM1}' is paused; saving it, and the next 'up' "
                f"brings it back running") in _info(mgr)

    def test_a_running_vm_is_saved(self):
        mgr, session = _manager({VM1: 'running'})

        _down(mgr)

        assert session.saved == [VM1]

    def test_only_a_vm_down_tried_and_failed_to_save_fails_the_run(self):
        """The shut-off VM is neither tried nor named in the failure."""
        mgr, session = _manager({VM1: 'shut off', VM2: 'running'},
                                vms=(VM1, VM2), save_ok=False)

        with pytest.raises(ProvisionError) as exc:
            _down(mgr)

        message = str(exc.value)
        assert session.saved == [VM2]
        assert f"could not bring down 1 VM(s) ({VM2}); their state is " \
            f"undefined" in message
        assert VM1 not in message

    def test_compose_clusters_are_stopped_when_every_vm_was_already_down(
            self):
        mgr, session = _manager({VM1: 'shut off', VM2: 'managedsave'},
                                vms=(VM1, VM2))
        stopped = []
        mgr.stop_compose_clusters = lambda: stopped.append(True)

        _down(mgr)

        assert session.saved == []
        assert stopped == [True]

    @pytest.mark.parametrize('state', ['crashed', 'in shutdown', 'pmsuspended'])
    def test_a_vm_in_any_other_state_is_tried_and_its_refusal_reported(
            self, state):
        """
        Only shut off and saved are down. Anything else is handed to the
        save, whose refusal is a failure: a crashed guest is not down.
        """
        mgr, session = _manager({VM1: state})

        with pytest.raises(ProvisionError, match='state is undefined'):
            _down(mgr)

        assert session.saved == [VM1]

    def test_a_vm_libvirt_does_not_list_is_tried(self):
        mgr, session = _manager({})

        with pytest.raises(ProvisionError, match='could not bring down'):
            _down(mgr)

        assert session.saved == [VM1]

    def test_states_that_cannot_be_read_leave_every_vm_to_be_tried(self):
        """
        No VM counts as down without libvirt saying so. Here it is in fact
        shut off, the save refuses it, and the run fails as it did before.
        """
        mgr, session = _manager(
            {VM1: 'shut off'},
            states_error=ProvisionError('could not query VM states'))

        with pytest.raises(ProvisionError, match='could not bring down'):
            _down(mgr)

        assert session.saved == [VM1]


class TestDownSuspendLeavesAVmAlreadyDownAsItIs:

    def test_a_paused_vm_is_left_as_it_is(self):
        """``--suspend`` would have paused it: it is already there."""
        mgr, session = _manager({VM1: 'paused'})

        _down(mgr, suspend=True)

        assert session.suspended == []
        assert f"VM '{VM1}' is already suspended; leaving it as it is" \
            in _info(mgr)

    @pytest.mark.parametrize('state, what', [('shut off', 'shut off'),
                                             ('managedsave', 'saved')])
    def test_a_shut_off_or_saved_vm_is_left_as_it_is(self, state, what):
        mgr, session = _manager({VM1: state})

        _down(mgr, suspend=True)

        assert session.suspended == []
        assert f"VM '{VM1}' is already {what}; leaving it as it is" \
            in _info(mgr)

    def test_a_running_vm_is_suspended(self):
        mgr, session = _manager({VM1: 'running'})

        _down(mgr, suspend=True)

        assert session.suspended == [VM1]
