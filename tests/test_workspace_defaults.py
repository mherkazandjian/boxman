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


class TestPerClusterInventoryGeneration:
    """
    Each cluster gets its own inventory/01-hosts.yml containing ONLY that
    cluster's hosts, so a per-cluster Ansible run never sees another cluster's
    hosts under groups['all'] (the multi-cluster isolation fix).
    """

    def test_per_cluster_inventory_generated_in_cluster_files(self):
        """A cluster's own 01-hosts.yml is auto-generated under its files."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {
                    'vms': {
                        'node01': {'hostname': 'node01'},
                        'node02': {'hostname': 'node02'},
                    },
                },
            },
        }
        mgr = _make_manager(config)
        cfiles = config['clusters']['c1']['files']
        assert 'inventory/01-hosts.yml' in cfiles
        parsed = yaml.safe_load(cfiles['inventory/01-hosts.yml'])
        assert set(parsed['all']['hosts'].keys()) == {'c1_node01', 'c1_node02'}
        assert 'c1' in parsed['all']['children']

    def test_per_cluster_inventory_only_own_hosts(self):
        """Each cluster's inventory excludes the other cluster's hosts."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'cluster_1': {'vms': {'service01': {}, 'node01': {}}},
                'cluster_2': {'vms': {'service01': {}, 'node01': {}}},
            },
        }
        mgr = _make_manager(config)
        inv1 = yaml.safe_load(
            config['clusters']['cluster_1']['files']['inventory/01-hosts.yml'])
        inv2 = yaml.safe_load(
            config['clusters']['cluster_2']['files']['inventory/01-hosts.yml'])
        assert set(inv1['all']['hosts']) == {'cluster_1_service01', 'cluster_1_node01'}
        assert set(inv2['all']['hosts']) == {'cluster_2_service01', 'cluster_2_node01'}
        # no cross-cluster leakage
        assert all('cluster_2' not in h for h in inv1['all']['hosts'])
        assert all('cluster_1' not in h for h in inv2['all']['hosts'])
        # only the owning cluster's group appears
        assert set(inv1['all']['children']) == {'cluster_1'}
        assert set(inv2['all']['children']) == {'cluster_2'}

    def test_per_cluster_aliases_match_combined(self):
        """
        A host's boxman_alias is identical in the per-cluster inventory and the
        combined workspace inventory (consistent, project-wide numbering).
        """
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'cluster_1': {'vms': {'service01': {}, 'node01': {}}},
                'cluster_2': {'vms': {'service01': {}, 'node01': {}}},
            },
        }
        mgr = _make_manager(config)
        combined = yaml.safe_load(
            config['workspace']['files']['inventory/01-hosts.yml'])
        inv2 = yaml.safe_load(
            config['clusters']['cluster_2']['files']['inventory/01-hosts.yml'])
        # cluster_2 hosts come after cluster_1 in the global ordering → node2/node3
        assert inv2['all']['hosts']['cluster_2_service01']['boxman_alias'] == 'node2'
        assert (inv2['all']['hosts']['cluster_2_service01']['boxman_alias']
                == combined['all']['hosts']['cluster_2_service01']['boxman_alias'])

    def test_per_cluster_inventory_honors_cluster_inventory_override(self):
        """A cluster's `inventory:` key relocates its generated 01-hosts.yml."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'cluster_2': {
                    'inventory': 'inventory_cluster_2',
                    'vms': {'node01': {}},
                },
            },
        }
        mgr = _make_manager(config)
        cfiles = config['clusters']['cluster_2']['files']
        assert 'inventory_cluster_2/01-hosts.yml' in cfiles
        assert 'inventory/01-hosts.yml' not in cfiles

    def test_per_cluster_inventory_not_overwritten_if_explicit(self):
        """A user-provided cluster inventory file is preserved."""
        custom = "---\nall:\n  hosts:\n    mine:\n"
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {
                    'files': {'inventory/01-hosts.yml': custom},
                    'vms': {'node01': {}},
                },
            },
        }
        mgr = _make_manager(config)
        assert config['clusters']['c1']['files']['inventory/01-hosts.yml'] == custom

    def test_no_per_cluster_inventory_without_workdir(self):
        """A cluster with no resolvable workdir gets no generated inventory."""
        config = {
            'clusters': {
                'orphan': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        assert 'files' not in config['clusters']['orphan']


class TestAnsibleCfgGeneration:

    def test_ansible_cfg_generated_at_workspace_level(self):
        """ansible.cfg is auto-generated in workspace.files with expected sections."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        cfg = config['workspace']['files']['ansible.cfg']
        assert '[defaults]' in cfg
        assert 'host_key_checking = False' in cfg
        assert 'forks = 10' in cfg
        assert 'gathering = smart' in cfg
        assert 'fact_caching = jsonfile' in cfg
        assert '[ssh_connection]' in cfg
        assert 'pipelining = True' in cfg
        # ansible.cfg should NOT be in the cluster files
        assert 'ansible.cfg' not in config['clusters']['c1'].get('files', {})

    def test_ansible_cfg_not_overwritten_if_explicit(self):
        """User-provided ansible.cfg in workspace.files is preserved."""
        custom_cfg = "[defaults]\nmy_custom = true\n"
        config = {
            'workspace': {
                'path': '/tmp/ws',
                'files': {'ansible.cfg': custom_cfg},
            },
            'clusters': {
                'c1': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        assert config['workspace']['files']['ansible.cfg'] == custom_cfg


class TestEnvShGeneration:

    def test_env_sh_generated_at_workspace_level(self):
        """env.sh is auto-generated in workspace.files with expected exports."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {'vms': {'node01': {}, 'node02': {}}},
            },
        }
        mgr = _make_manager(config)
        env_sh = config['workspace']['files']['env.sh']
        assert 'export INVENTORY=inventory' in env_sh
        assert 'export SSH_CONFIG=ssh_config' in env_sh
        assert 'export GATEWAYHOST=c1_node01' in env_sh
        assert 'export ANSIBLE_CONFIG=ansible.cfg' in env_sh
        assert 'export ANSIBLE_INVENTORY="$INVENTORY"' in env_sh
        assert 'export ANSIBLE_SSH_ARGS="-F $SSH_CONFIG"' in env_sh
        # env.sh should NOT be in the cluster files
        assert 'env.sh' not in config['clusters']['c1'].get('files', {})

    def test_env_sh_gateway_is_first_vm(self):
        """GATEWAYHOST is set to the first VM in the cluster."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {'vms': {'alpha': {}, 'beta': {}, 'gamma': {}}},
            },
        }
        mgr = _make_manager(config)
        env_sh = config['workspace']['files']['env.sh']
        assert 'export GATEWAYHOST=c1_alpha' in env_sh

    def test_env_sh_paths_are_relative(self):
        """SSH_CONFIG and ANSIBLE_CONFIG are relative paths (next to env.sh)."""
        config = {
            'workspace': {'path': '~/my/workspace'},
            'clusters': {
                'c1': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        env_sh = config['workspace']['files']['env.sh']
        assert 'export SSH_CONFIG=ssh_config' in env_sh
        assert 'export ANSIBLE_CONFIG=ansible.cfg' in env_sh

    def test_env_sh_not_overwritten_if_explicit(self):
        """User-provided env.sh in workspace.files is preserved."""
        custom_env = "export FOO=bar\n"
        config = {
            'workspace': {
                'path': '/tmp/ws',
                'files': {'env.sh': custom_env},
            },
            'clusters': {
                'c1': {
                    'vms': {'vm01': {}},
                },
            },
        }
        mgr = _make_manager(config)
        assert config['workspace']['files']['env.sh'] == custom_env


class TestWorkspaceInventoryGeneration:

    def test_workspace_inventory_generated(self):
        """inventory/01-hosts.yml is auto-generated in workspace.files."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'c1': {'vms': {'node01': {}, 'node02': {}}},
            },
        }
        mgr = _make_manager(config)
        inv = config['workspace']['files']['inventory/01-hosts.yml']
        parsed = yaml.safe_load(inv)
        assert set(parsed['all']['hosts'].keys()) == {'c1_node01', 'c1_node02'}
        assert parsed['all']['hosts']['c1_node01'] == {'boxman_alias': 'node0'}
        assert parsed['all']['hosts']['c1_node02'] == {'boxman_alias': 'node1'}
        # cluster group
        assert 'c1' in parsed['all']['children']
        assert set(parsed['all']['children']['c1']['hosts'].keys()) == {'c1_node01', 'c1_node02'}

    def test_workspace_inventory_aggregates_all_clusters(self):
        """Workspace-level inventory includes VMs from all clusters."""
        config = {
            'workspace': {'path': '/tmp/ws'},
            'clusters': {
                'web': {'vms': {'web01': {}, 'web02': {}}},
                'db': {'vms': {'db01': {}}},
            },
        }
        mgr = _make_manager(config)
        inv = config['workspace']['files']['inventory/01-hosts.yml']
        parsed = yaml.safe_load(inv)
        assert set(parsed['all']['hosts'].keys()) == {'web_web01', 'web_web02', 'db_db01'}
        assert parsed['all']['hosts']['web_web01'] == {'boxman_alias': 'node0'}
        assert parsed['all']['hosts']['web_web02'] == {'boxman_alias': 'node1'}
        assert parsed['all']['hosts']['db_db01'] == {'boxman_alias': 'node2'}
        # cluster groups
        assert set(parsed['all']['children'].keys()) == {'web', 'db'}
        assert set(parsed['all']['children']['web']['hosts'].keys()) == {'web_web01', 'web_web02'}
        assert set(parsed['all']['children']['db']['hosts'].keys()) == {'db_db01'}

    def test_workspace_inventory_not_overwritten_if_explicit(self):
        """User-provided workspace inventory is preserved."""
        custom_inv = "---\nall:\n  hosts:\n    custom01:\n"
        config = {
            'workspace': {
                'path': '/tmp/ws',
                'files': {'inventory/01-hosts.yml': custom_inv},
            },
            'clusters': {
                'c1': {'vms': {'vm01': {}}},
            },
        }
        mgr = _make_manager(config)
        assert config['workspace']['files']['inventory/01-hosts.yml'] == custom_inv


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
        """Only missing workspace files are auto-generated; explicit ones are kept."""
        custom_cfg = "[defaults]\ncustom = yes\n"
        config = {
            'workspace': {
                'path': '/tmp/ws',
                'files': {'ansible.cfg': custom_cfg},
            },
            'clusters': {
                'c1': {
                    'vms': {'vm01': {}, 'vm02': {}},
                },
            },
        }
        mgr = _make_manager(config)
        ws_files = config['workspace']['files']
        # ansible.cfg kept as-is
        assert ws_files['ansible.cfg'] == custom_cfg
        # env.sh and inventory auto-generated at workspace level
        assert 'env.sh' in ws_files
        assert 'inventory/01-hosts.yml' in ws_files
