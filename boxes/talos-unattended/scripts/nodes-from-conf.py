#!/usr/bin/env python3
"""
Print the Talos node table that gen-config.sh reads, from this box's conf.yml.

One line per node, whitespace separated:

    <name> <controlplane|worker> <ipv4> <mac>

The address comes from the fabric network's DHCP reservation for the MAC the
VM pins, so the table cannot drift from what boxman configures. The role comes
from the VM name (cp-NN / worker-NN): conf.yml stays on the existing schema,
which has no role key. Runs on the host, where boxman is installed:

    python3 scripts/nodes-from-conf.py [path/to/conf.yml] > nodes.txt
"""

from __future__ import annotations

import os
import sys

import yaml

from boxman.utils.jinja_env import create_jinja_env

ROLES = {"cp": "controlplane", "worker": "worker"}


def render(conf_path: str) -> dict:
    conf_path = os.path.abspath(conf_path)
    os.environ["BOXMAN_CONF_DIR"] = os.path.dirname(conf_path)
    env = create_jinja_env(os.path.dirname(conf_path))
    return yaml.safe_load(env.get_template(os.path.basename(conf_path)).render())


def node_table(cfg: dict) -> list[tuple[str, str, str, str]]:
    fabric = cfg["clusters"]["management"]["networks"]["fabric"]
    by_mac = {h["mac"].lower(): h for h in fabric["ip"]["dhcp"]["hosts"]}
    rows = []
    for name, vm in cfg["clusters"]["talos"]["vms"].items():
        prefix = name.split("-", 1)[0]
        if prefix not in ROLES:
            raise SystemExit(f"{name}: cannot tell its role (expected cp-NN or worker-NN)")
        nics = vm.get("networks") or []
        if len(nics) != 1 or nics[0].get("name") != "management::fabric" or not nics[0].get("mac"):
            raise SystemExit(f"{name}: needs exactly one NIC on management::fabric with a pinned mac")
        mac = nics[0]["mac"].lower()
        if mac not in by_mac:
            raise SystemExit(f"{name}: mac {mac} has no DHCP reservation on fabric")
        if by_mac[mac].get("name") != name:
            raise SystemExit(f"{name}: mac {mac} is reserved for {by_mac[mac].get('name')}")
        rows.append((name, ROLES[prefix], by_mac[mac]["ip"], mac))
    return rows


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conf = argv[1] if len(argv) > 1 else os.path.join(here, "conf.yml")
    for row in node_table(render(conf)):
        print(" ".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
