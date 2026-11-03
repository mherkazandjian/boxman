#!/usr/bin/env python3
"""
Print the facts the install driver needs from this box's rendered conf.yml.

Runs on the host, where boxman is installed, with the same environment as
``boxman up`` (TALOS_PROFILE, TALOS_PROJECT, TALOS_LAB_DIR, ...):

    python3 scripts/box-facts.py [path/to/conf.yml]

One fact per line, whitespace separated, no value contains whitespace:

    project   <name>
    uri       <libvirt uri>
    use_sudo  <true|false>
    workspace <path>
    admin     <control vm's ssh user>
    network   <libvirt network> <gateway address> <netmask>
    template  <name> <workdir> <disk bytes>
    vm        <domain> <cluster> <vm> <vcpus> <memory MiB> <disk bytes> <mac|-> <role|->

The ``vm`` lines come in config order: the management group's VMs, then the
Talos nodes. ``role`` is ``control`` for the management VM and the Talos role
(from the cp-NN / worker-NN name, as nodes-from-conf.py reads it) for the
others. Anything this script cannot account for is an error: the driver
must never act on a VM it did not expect.
"""

from __future__ import annotations

import os
import re
import sys

import yaml

from boxman.utils.jinja_env import create_jinja_env

ROLES = {"cp": "controlplane", "worker": "worker"}
SIZE = re.compile(r"^(\d+)([KMGT]?)$")
UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def render(conf_path: str) -> dict:
    conf_path = os.path.abspath(conf_path)
    os.environ["BOXMAN_CONF_DIR"] = os.path.dirname(conf_path)
    env = create_jinja_env(os.path.dirname(conf_path))
    return yaml.safe_load(env.get_template(os.path.basename(conf_path)).render())


def size_bytes(value) -> int:
    match = SIZE.match(str(value).strip().upper())
    if not match:
        raise SystemExit(f"cannot read the disk size {value!r}")
    return int(match.group(1)) * UNITS[match.group(2)]


def vcpus(vm: dict) -> int:
    if "vcpus" in vm:
        return int(vm["vcpus"])
    cpus = vm.get("cpus") or {}
    return int(cpus.get("sockets", 1)) * int(cpus.get("cores", 1)) * int(cpus.get("threads", 1))


def facts(cfg: dict) -> list[str]:
    project = cfg["project"]
    prefix = f"bprj__{project}__bprj"
    libvirt = (cfg.get("provider") or {}).get("libvirt") or {}
    use_sudo = libvirt.get("use_sudo", False)
    if not isinstance(use_sudo, bool):
        raise SystemExit(f"provider.libvirt.use_sudo must be true or false, got {use_sudo!r}")
    clusters = cfg.get("clusters") or {}
    if set(clusters) != {"management", "talos"}:
        raise SystemExit(f"expected the clusters management and talos, got {sorted(clusters)}")
    mgmt = clusters["management"]
    out = [
        f"project {project}",
        f"uri {libvirt.get('uri', '')}",
        f"use_sudo {'true' if use_sudo else 'false'}",
        f"workspace {os.path.expanduser(cfg['workspace']['path'])}",
        f"admin {mgmt['admin_user']}",
    ]
    nets = mgmt.get("networks") or {}
    if list(nets) != ["fabric"]:
        raise SystemExit(f"management must own exactly the fabric network, got {list(nets)}")
    fabric = nets["fabric"]
    network = f"{prefix}__clstr__management__clstr__fabric"
    out.append(f"network {network} {fabric['ip']['address']} {fabric['ip']['netmask']}")

    templates = cfg.get("templates") or {}
    by_name = {t["name"]: t for t in templates.values()}
    base = mgmt.get("base_image")
    if base not in by_name:
        raise SystemExit(f"management's base_image {base!r} is not a template of this box")
    template = by_name[base]
    template_dir = os.path.expanduser(template.get("workdir", "~/boxman-templates"))
    out.append(f"template {base} {template_dir} {size_bytes(template['disk_size'])}")

    for cluster in ("management", "talos"):
        for name, vm in (clusters[cluster].get("vms") or {}).items():
            domain = f"{prefix}_{cluster}_{name}"
            memory = int(vm.get("memory", template.get("memory", 0)))
            if cluster == "management":
                adapters = vm.get("network_adapters") or []
                if len(adapters) != 1 or adapters[0].get("network_source") != "fabric":
                    raise SystemExit(f"{name}: needs exactly one adapter on fabric")
                disk = size_bytes(template["disk_size"])
                out.append(f"vm {domain} {cluster} {name} {vcpus(vm)} {memory} {disk} - control")
                continue
            role = ROLES.get(name.split("-", 1)[0])
            if role is None:
                raise SystemExit(f"{name}: cannot tell its role (expected cp-NN or worker-NN)")
            nics = vm.get("networks") or []
            if len(nics) != 1 or nics[0].get("name") != "management::fabric" or not nics[0].get("mac"):
                raise SystemExit(f"{name}: needs exactly one NIC on management::fabric with a pinned mac")
            mac = nics[0]["mac"].lower()
            out.append(f"vm {domain} {cluster} {name} {vcpus(vm)} {memory} "
                       f"{size_bytes(vm['disk_size'])} {mac} {role}")
    for line in out:
        if any(not part or part.isspace() for part in line.split(" ")):
            raise SystemExit(f"a fact has an empty or blank value: {line!r}")
    return out


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conf = argv[1] if len(argv) > 1 else os.path.join(here, "conf.yml")
    for line in facts(render(conf)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
