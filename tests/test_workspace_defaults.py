"""
Tests for BoxmanManager.resolve_workspace_defaults().
"""

import os
import yaml
import pytest

from boxman.manager import BoxmanManager


def _make_manager(config):
    """Create a BoxmanManager with an in-memory config dict (no file loading)."""
    mgr = BoxmanManager()
    mgr.config = config
    mgr.resolve_workspace_defaults()
    return mgr


class TestWorkdirResolution:

    def test_workdir_derived_from_workspace_path(self):
        """Cluster without workdir gets workspace.path / cluster_name."""
        config = {
            'workspace': {'path': '~/workspaces/myproject'},
            'clusters': {
                'web': {'vms': {'web01': {}}},
            },
        }
        mgr = _make_manager(config)
        assert config['clusters']['web']['workdir'] == os.path.join(
            '~/workspaces/myproject', 'web'
        )

    def test_explicit_workdir_takes_precedence(self):
        """Cluster with explicit workdir is not overwritten."""
        config = {
            'workspace': {'path': '~/workspaces/myproject'},
            'clusters': {
                'db': {
                    'workdir': '~/custom/db-workdir',
                    'vms': {'db01': {}},
                },
            },
        }
        mgr = _make_manager(config)
        assert config['clusters']['db']['workdir'] == '~/custom/db-workdir'

    def test_multiple_clusters_get_own_workdir(self):
        """Each cluster gets its own subdirectory under workspace.path."""
        config = {
            'workspace': {'path': '/opt/boxman'},
            'clusters': {
                'alpha': {'vms': {'a01': {}}},
                'beta': {'vms': {'b01': {}}},
            },
        }
        mgr = _make_manager(config)
        assert config['clusters']['alpha']['workdir'] == '/opt/boxman/alpha'
        assert config['clusters']['beta']['workdir'] == '/opt/boxman/beta'

    def test_no_workspace_path_no_workdir_warns(self):
        """Cluster with no workdir and no workspace.path logs a warning and is skipped."""
        config = {
            'clusters': {
                'orphan': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        # no workdir should be set, no files generated
        assert 'workdir' not in config['clusters']['orphan']
        assert 'files' not in config['clusters']['orphan']


class TestInventoryGeneration:

    def test_inventory_contains_all_vms(self):
        """Auto-generated inventory lists every VM in the cluster."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {
                    'vms': {
                        'node01': {'hostname': 'node01'},
                        'node02': {'hostname': 'node02'},
                        'node03': {'hostname': 'node03'},
                    },
                },
            },
        }
        mgr = _make_manager(config)
        inv = config['clusters']['c1']['files']['inventory/01-hosts.yml']
        parsed = yaml.safe_load(inv)
        assert set(parsed['all']['hosts'].keys()) == {'node01', 'node02', 'node03'}

    def test_inventory_single_vm(self):
        """Inventory works with a single VM."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'solo': {'vms': {'only01': {}}},
            },
        }
        mgr = _make_manager(config)
        inv = config['clusters']['solo']['files']['inventory/01-hosts.yml']
        parsed = yaml.safe_load(inv)
        assert list(parsed['all']['hosts'].keys()) == ['only01']

    def test_inventory_not_overwritten_if_explicit(self):
        """User-provided inventory in files: block is preserved."""
        custom_inv = "---\nall:\n  hosts:\n    custom01:\n"
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {
                    'vms': {'node01': {}},
                    'files': {'inventory/01-hosts.yml': custom_inv},
                },
            },
        }
        mgr = _make_manager(config)
        assert config['clusters']['c1']['files']['inventory/01-hosts.yml'] == custom_inv


class TestAnsibleCfgGeneration:

    def test_ansible_cfg_generated(self):
        """ansible.cfg is auto-generated with expected sections."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        cfg = config['clusters']['c1']['files']['ansible.cfg']
        assert '[defaults]' in cfg
        assert 'host_key_checking = False' in cfg
        assert 'forks = 10' in cfg
        assert 'gathering = smart' in cfg
        assert 'fact_caching = jsonfile' in cfg
        assert '[ssh_connection]' in cfg
        assert 'pipelining = True' in cfg

    def test_ansible_cfg_not_overwritten_if_explicit(self):
        """User-provided ansible.cfg is preserved."""
        custom_cfg = "[defaults]\nmy_custom = true\n"
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {
                    'vms': {'vm01': {}},
                    'files': {'ansible.cfg': custom_cfg},
                },
            },
        }
        mgr = _make_manager(config)
        assert config['clusters']['c1']['files']['ansible.cfg'] == custom_cfg


class TestEnvShGeneration:

    def test_env_sh_generated(self):
        """env.sh is auto-generated with expected exports."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {'vms': {'node01': {}, 'node02': {}}},
            },
        }
        mgr = _make_manager(config)
        env_sh = config['clusters']['c1']['files']['env.sh']
        assert 'export INVENTORY=inventory' in env_sh
        assert 'export SSH_CONFIG=' in env_sh
        assert 'ssh_config' in env_sh
        assert 'export GATEWAYHOST=node01' in env_sh
        assert 'export ANSIBLE_CONFIG=' in env_sh
        assert 'ansible.cfg' in env_sh
        assert 'export ANSIBLE_INVENTORY="$INVENTORY"' in env_sh
        assert 'export ANSIBLE_SSH_ARGS="-F $SSH_CONFIG"' in env_sh

    def test_env_sh_gateway_is_first_vm(self):
        """GATEWAYHOST is set to the first VM in the cluster."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {'vms': {'alpha': {}, 'beta': {}, 'gamma': {}}},
            },
        }
        mgr = _make_manager(config)
        env_sh = config['clusters']['c1']['files']['env.sh']
        assert 'export GATEWAYHOST=alpha' in env_sh

    def test_env_sh_paths_use_expanded_workdir(self):
        """SSH_CONFIG and ANSIBLE_CONFIG paths use the expanded workdir."""
        config = {
            'workspace': {'path': '~/my/workspace'},
            'clusters': {
                'c1': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        env_sh = config['clusters']['c1']['files']['env.sh']
        expanded = os.path.expanduser('~/my/workspace/c1')
        assert f'export SSH_CONFIG={expanded}/ssh_config' in env_sh
        assert f'export ANSIBLE_CONFIG={expanded}/ansible.cfg' in env_sh

    def test_env_sh_not_overwritten_if_explicit(self):
        """User-provided env.sh is preserved."""
        custom_env = "export FOO=bar\n"
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {
                    'vms': {'vm01': {}},
                    'files': {'env.sh': custom_env},
                },
            },
        }
        mgr = _make_manager(config)
        assert config['clusters']['c1']['files']['env.sh'] == custom_env


class TestEdgeCases:

    def test_cluster_with_no_vms_skipped(self):
        """Cluster with empty vms dict gets no auto-generated files."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'empty': {'vms': {}},
            },
        }
        mgr = _make_manager(config)
        assert 'files' not in config['clusters']['empty']

    def test_cluster_without_vms_key_skipped(self):
        """Cluster without a vms key gets no auto-generated files."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'novms': {},
            },
        }
        mgr = _make_manager(config)
        assert 'files' not in config['clusters']['novms']

    def test_no_clusters_key(self):
        """Config with no clusters key does not raise."""
        config = {'workspace': {'path': '/tmp/ws'}}
        mgr = _make_manager(config)  # should not raise

    def test_partial_explicit_files_are_preserved(self):
        """Only missing files are auto-generated; explicit ones are kept."""
        custom_cfg = "[defaults]\ncustom = yes\n"
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {
                    'vms': {'vm01': {}, 'vm02': {}},
                    'files': {'ansible.cfg': custom_cfg},
                },
            },
        }
        mgr = _make_manager(config)
        files = config['clusters']['c1']['files']
        # ansible.cfg kept as-is
        assert files['ansible.cfg'] == custom_cfg
        # inventory and env.sh auto-generated
        assert 'inventory/01-hosts.yml' in files
        assert 'env.sh' in files
        inv = yaml.safe_load(files['inventory/01-hosts.yml'])
        assert set(inv['all']['hosts'].keys()) == {'vm01', 'vm02'}
