"""
#247 -- every name boxman generates for a VM is one ``ssh_config`` resolves.

``ssh_config`` names a VM's ``Host`` block ``<cluster>_<hostname>`` (the VM
key when ``hostname:`` is absent or null), plus a padded ``node<N>``. The
inventory host key and ``env.sh``'s ``GATEWAYHOST`` used ``<cluster>_<vm
key>``, so for a VM whose ``hostname:`` differs from its key a bare
``boxman ssh`` -- ``ssh -F ssh_config $GATEWAYHOST`` -- found no ``Host``
block ("Could not resolve hostname"), and neither did Ansible through the
generated inventory and ``ANSIBLE_SSH_ARGS="-F $SSH_CONFIG"``.

The inventory host key stays ``<cluster>_<vm key>``: renaming it would move
``host_vars`` and ``--limit`` patterns, and two VMs may declare the same
hostname, where it is the only unique name. A VM whose ``hostname:`` differs
from its key gets ``ansible_host: <cluster>_<hostname>``, its ssh alias, and
``GATEWAYHOST`` is the first libvirt VM's ssh alias. Nothing changes where the
two names agree.

Host-side only: the ssh_config comes from the real ``write_ssh_config``, with
the libvirt session standing in for addresses.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from conftest import make_bare_manager

pytestmark = pytest.mark.unit


def _generate(tmp_path: Path, clusters: dict):
    """Generate the workspace files and the ssh_config for *clusters*.

    Returns ``(env, inventories, aliases)``: the ``env.sh`` variables, every
    generated inventory (the combined one first) parsed, and the ``Host``
    aliases of each ``ssh_config`` block, in order.
    """
    for cluster in clusters.values():
        if "vms" in cluster:
            cluster.setdefault("admin_pass", "secret")
            cluster.setdefault("ssh_config", "ssh_config")
    config = {"project": "p", "workspace": {"path": str(tmp_path)}, "clusters": clusters}
    if any("boxes" in cluster for cluster in clusters.values()):
        config["version"] = "2.0"  # docker-compose clusters need it
    manager = make_bare_manager(config)
    manager.resolve_workspace_defaults()

    session = MagicMock()
    session.get_vm_ip_addresses.side_effect = lambda name: {"eth0": "192.168.10.10"}
    manager.session_for_cluster = lambda cluster_name: session
    manager._docker_ssh_jump_stanza = lambda: None
    manager.write_ssh_config()

    ws_files = config["workspace"]["files"]
    env = dict(line.removeprefix("export ").split("=", 1)
               for line in ws_files["env.sh"].splitlines())
    inventories = [yaml.safe_load(ws_files["inventory/01-hosts.yml"])]
    for cluster in clusters.values():
        for key, text in (cluster.get("files") or {}).items():
            if key.endswith("01-hosts.yml"):
                inventories.append(yaml.safe_load(text))
    aliases = [line.split()[1:]
               for line in (tmp_path / "ssh_config").read_text().splitlines()
               if line.startswith("Host ")]
    return env, inventories, aliases


def _ssh_targets(inventory: dict) -> dict[str, str]:
    """Each inventory host, and the name ssh is given for it."""
    return {host: (hostvars or {}).get("ansible_host", host)
            for host, hostvars in inventory["all"]["hosts"].items()}


def _every_alias(aliases) -> set[str]:
    return {alias for block in aliases for alias in block}


class TestAHostnameThatDiffersFromItsKey:

    CLUSTERS = {"lab": {"vms": {"vm01": {"hostname": "myvm"}}}}

    def test_gatewayhost_is_an_alias_of_that_vm(self, tmp_path):
        env, _inventories, aliases = _generate(tmp_path, self.CLUSTERS)

        assert aliases == [["lab_myvm", "node0"]]
        assert env["GATEWAYHOST"] == "lab_myvm"

    def test_every_inventory_host_reaches_an_alias(self, tmp_path):
        _env, inventories, aliases = _generate(tmp_path, self.CLUSTERS)

        # the combined inventory and the cluster's own
        assert len(inventories) == 2
        for inventory in inventories:
            # the host key stays the VM key's; ssh is given the alias
            assert _ssh_targets(inventory) == {"lab_vm01": "lab_myvm"}
            assert set(_ssh_targets(inventory).values()) <= _every_alias(aliases)


@pytest.mark.parametrize("vm", [{}, {"hostname": None}, {"hostname": "vm01"}],
                         ids=["absent", "null", "equal-to-the-key"])
def test_nothing_changes_where_the_names_agree(vm, tmp_path):
    env, inventories, aliases = _generate(tmp_path, {"lab": {"vms": {"vm01": vm}}})

    assert aliases == [["lab_vm01", "node0"]]
    assert env["GATEWAYHOST"] == "lab_vm01"
    for inventory in inventories:
        # no ansible_host: the host key is the alias
        assert inventory["all"]["hosts"] == {
            "lab_vm01": {"boxman_alias": "node0", "ansible_host_key_checking": False}}


def test_the_gateway_stays_the_first_vm_under_its_alias(tmp_path):
    env, inventories, aliases = _generate(tmp_path, {
        "web": {"vms": {"w1": {"hostname": "front"}, "w2": {}}},
        "db": {"vms": {"d1": {"hostname": "pg"}}},
    })

    assert aliases == [["web_front", "node0"], ["web_w2", "node1"], ["db_pg", "node2"]]
    assert env["GATEWAYHOST"] == "web_front"
    # the combined inventory, then each cluster's own: each host reaches
    # its own VM's alias, not just some alias of the project's
    assert [_ssh_targets(inventory) for inventory in inventories] == [
        {"web_w1": "web_front", "web_w2": "web_w2", "db_d1": "db_pg"},
        {"web_w1": "web_front", "web_w2": "web_w2"},
        {"db_d1": "db_pg"},
    ]


def test_two_vms_that_declare_one_hostname_stay_two_hosts(tmp_path):
    """The VM key is the only name unique in a cluster: inventory keys stay theirs."""
    _env, inventories, _aliases = _generate(tmp_path, {
        "lab": {"vms": {"a": {"hostname": "same"}, "b": {"hostname": "same"}}}})

    assert _ssh_targets(inventories[0]) == {"lab_a": "lab_same", "lab_b": "lab_same"}


def test_an_alias_is_written_into_the_inventory_as_yaml(tmp_path):
    """A hostname holding a quote or a backslash still parses as itself.

    ``write_ssh_config`` refuses such an alias, and cloning refuses such a
    hostname, but the inventory is generated before either runs.
    """
    clusters = {"lab": {"vms": {"vm01": {"hostname": 'a"b\\c'}}}}
    config = {"project": "p", "workspace": {"path": str(tmp_path)}, "clusters": clusters}
    manager = make_bare_manager(config)
    manager.resolve_workspace_defaults()

    inventory = yaml.safe_load(config["workspace"]["files"]["inventory/01-hosts.yml"])
    assert _ssh_targets(inventory) == {"lab_vm01": 'lab_a"b\\c'}


def _node_aliases(inventory: dict) -> dict[str, str]:
    return {host: hostvars["boxman_alias"]
            for host, hostvars in inventory["all"]["hosts"].items()}


def _compose_cluster(*boxes):
    return {"provider": "docker-compose", "boxes": {box: {"image": "busybox"} for box in boxes}}


class TestAVmsBoxmanAliasIsItsNodeAlias:
    """The inventory's ``boxman_alias`` is the ``node<N>`` of the VM's ssh_config
    block (#247, found with it). The inventory numbered docker-compose boxes
    too, and padded to the host count, so in a hybrid project a VM's
    ``boxman_alias`` named another VM's alias, or none."""

    @staticmethod
    def _generate(tmp_path, clusters):
        _env, inventories, aliases = _generate(tmp_path, clusters)
        node_of = {block[0]: block[-1] for block in aliases}
        return inventories, node_of

    def test_with_the_containers_listed_first(self, tmp_path):
        inventories, node_of = self._generate(tmp_path, {
            "apps": _compose_cluster("web", "db"),
            "lab": {"vms": {"vm01": {"hostname": "myvm"}, "vm02": {}}},
        })

        assert node_of == {"lab_myvm": "node0", "lab_vm02": "node1"}
        # the VMs as in ssh_config; the containers, which have no ssh alias,
        # after them
        assert _node_aliases(inventories[0]) == {
            "apps_web": "node2", "apps_db": "node3", "lab_vm01": "node0", "lab_vm02": "node1"}

    def test_when_the_hosts_need_wider_numbers_than_the_vms(self, tmp_path):
        vms = {f"vm{i}": {} for i in range(9)}
        inventories, node_of = self._generate(tmp_path, {
            "lab": {"vms": vms}, "apps": _compose_cluster("web", "db")})

        assert node_of["lab_vm8"] == "node8"
        assert _node_aliases(inventories[0]) == {
            **{f"lab_vm{i}": f"node{i}" for i in range(9)},
            "apps_web": "node09", "apps_db": "node10"}


class TestEverySiteThatNamesTheVmForSsh:
    """The key push and ``connect_info`` name the VM by its alias too."""

    @staticmethod
    def _manager(tmp_path: Path):
        manager = make_bare_manager({
            "project": "p", "workspace": {"path": str(tmp_path)},
            "clusters": {"lab": {"vms": {"vm01": {"hostname": "myvm"}},
                                 "admin_pass": "secret", "ssh_config": "ssh_config"}},
        })
        session = MagicMock()
        session.get_vm_ip_addresses.side_effect = lambda name: {"eth0": "192.168.10.10"}
        manager.session_for_cluster = lambda cluster_name: session
        manager._docker_ssh_jump_stanza = lambda: None
        # connect_info reads the VMs' states; unknown, it asks every VM
        manager._vm_states_if_known = lambda: None
        return manager

    def test_the_key_push(self, tmp_path):
        (tmp_path / "id_ed25519_boxman.pub").write_text("ssh-ed25519 AAAA\n")
        manager = self._manager(tmp_path)
        manager._try_add_ssh_key = MagicMock(return_value=True)

        assert manager.add_ssh_keys_to_vms() is True

        assert [c.kwargs["hostname"] for c in manager._try_add_ssh_key.call_args_list] == [
            "lab_myvm"]

    def test_connect_info(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.logger = MagicMock()

        manager.connect_info()

        said = [c.args[0] for c in manager.logger.status.call_args_list]
        via_config = [line for line in said if "via config:" in line]
        assert len(via_config) == 1 and via_config[0].endswith(" lab_myvm")


@pytest.mark.parametrize("workspace_path", [True, False], ids=["workspace-path", "workdirs"])
def test_node_numbers_follow_the_files_they_are_written_to(workspace_path, tmp_path):
    """``write_ssh_config`` numbers VMs file by file; a cluster that names an
    ssh_config file of its own between two that share one comes last. The
    shared numbering must keep that, or ``ssh node2`` reaches another VM
    (#247 review)."""
    shared, other = tmp_path / "shared", tmp_path / "other"
    clusters = {
        "a": {"vms": {"a": {}}},
        "b": {"vms": {"b": {}}, "ssh_config": "other_ssh_config"},
        "c": {"vms": {"c1": {}, "c2": {}}},
    }
    for name, cluster in clusters.items():
        cluster.setdefault("ssh_config", "ssh_config")
        cluster["admin_pass"] = "secret"
        if not workspace_path:
            cluster["workdir"] = str(other if name == "b" else shared)
    config = {"project": "p", "clusters": clusters,
              "workspace": {"path": str(shared)} if workspace_path else {}}
    shared.mkdir()
    other.mkdir()
    manager = make_bare_manager(config)
    manager.resolve_workspace_defaults()
    session = MagicMock()
    session.get_vm_ip_addresses.side_effect = lambda name: {"eth0": "192.168.10.10"}
    manager.session_for_cluster = lambda cluster_name: session
    manager._docker_ssh_jump_stanza = lambda: None
    manager.write_ssh_config()

    def host_lines(path):
        return [line.split()[1:] for line in path.read_text().splitlines()
                if line.startswith("Host ")]

    # as f06c8fe wrote them
    assert host_lines(shared / "ssh_config") == [
        ["a_a", "node0"], ["c_c1", "node1"], ["c_c2", "node2"]]
    b_file = shared / "other_ssh_config" if workspace_path else other / "other_ssh_config"
    assert host_lines(b_file) == [["b_b", "node3"]]
    # and the inventory names the same ones
    inventory = yaml.safe_load(config["workspace"]["files"]["inventory/01-hosts.yml"])
    assert _node_aliases(inventory) == {
        "a_a": "node0", "b_b": "node3", "c_c1": "node1", "c_c2": "node2"}


class TestANumericIdReachesTheAlias:
    """``boxman ssh <id>``, the id ``boxman ps`` shows, named the VM by its
    key, which ssh_config does not know when ``hostname:`` differs (#247
    review)."""

    @staticmethod
    def _manager():
        return make_bare_manager({
            "project": "p",
            "clusters": {"lab": {"vms": {"vm00": {}, "vm01": {"hostname": "myvm"}}}},
        })

    def test_resolve_vm_name(self):
        manager = self._manager()
        assert [manager.resolve_vm_name(i) for i in ("0", "1", "lab_vm01")] == [
            "lab_vm00", "lab_myvm", "lab_vm01"]

    def test_boxman_ssh(self, monkeypatch):
        from types import SimpleNamespace

        runner = MagicMock()
        runner.return_value.ssh_to_host.return_value = 0
        monkeypatch.setattr("boxman.manager_parts.misc.TaskRunner", runner)

        self._manager().ssh_session(SimpleNamespace(vm_name="1", cluster=None))

        runner.return_value.ssh_to_host.assert_called_once_with("lab_myvm")
