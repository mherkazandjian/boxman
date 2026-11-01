#!/usr/bin/env python3
"""
Check that a merged Talos machine config kept what patches may not change.

    check-node-config.py BASE FINAL NAME ROLE MAC VIP

BASE is the role config ``talosctl gen config`` produced from this cluster's
secrets bundle; FINAL is that config after every patch was applied. Schema
validation cannot tell whether a valid patch copied from another cluster
replaced this one's identity, so gen-config.sh runs this after each merge and
publishes nothing if it fails. FINAL must have:

- the same cluster identity, trust and install inputs as BASE (PROTECTED);
- machine.type ROLE, hostname NAME, and exactly one interface: the one
  selected by MAC, on DHCP, carrying the VIP if and only if ROLE is
  controlplane;
- no config document BASE does not have: a HostnameConfig or VIP document,
  say, could otherwise override the checks above from beside them.

Only the names of violated settings are printed, never their values: the
values include private keys. Exit status 1 on any violation.
"""

from __future__ import annotations

import sys

import yaml

#: v1alpha1 paths that must come through every patch unchanged
PROTECTED = (
    "machine.type",
    "machine.token",
    "machine.ca",
    "machine.acceptedCAs",
    "machine.install.disk",
    "machine.install.image",
    "cluster.id",
    "cluster.secret",
    "cluster.clusterName",
    "cluster.controlPlane.endpoint",
    "cluster.token",
    "cluster.ca",
    "cluster.aggregatorCA",
    "cluster.serviceAccount",
    "cluster.secretboxEncryptionSecret",
    "cluster.etcd.ca",
    "cluster.network.podSubnets",
    "cluster.network.serviceSubnets",
)

_MISSING = object()


def _get(doc, path):
    for key in path.split("."):
        if not isinstance(doc, dict) or key not in doc:
            return _MISSING
        doc = doc[key]
    return doc


def _load(path):
    with open(path, encoding="utf-8") as fobj:
        docs = [d for d in yaml.safe_load_all(fobj) if d is not None]
    main = [d for d in docs if isinstance(d, dict) and d.get("version") == "v1alpha1"]
    if len(main) != 1:
        raise SystemExit(f"{path}: expected exactly one v1alpha1 document, found {len(main)}")
    others = {(d.get("apiVersion"), d.get("kind"), d.get("name"))
              for d in docs if d is not main[0] and isinstance(d, dict)}
    return main[0], others


def problems(base_path, final_path, name, role, mac, vip):
    base, base_docs = _load(base_path)
    final, final_docs = _load(final_path)
    found = []
    if _get(base, "machine.type") != role:
        found.append(f"the generated base is not a {role} config")
    for path in PROTECTED:
        if _get(base, path) != _get(final, path):
            found.append(f"{path} was changed by a patch")
    if _get(final, "machine.network.hostname") != name:
        found.append("machine.network.hostname is not the node's name")
    interfaces = _get(final, "machine.network.interfaces")
    if not isinstance(interfaces, list) or len(interfaces) != 1:
        found.append("machine.network.interfaces must hold exactly the node's one interface")
    else:
        nic = interfaces[0] if isinstance(interfaces[0], dict) else {}
        if str(_get(nic, "deviceSelector.hardwareAddr")).lower() != mac.lower():
            found.append("the interface is not selected by the node's mac")
        if nic.get("dhcp") is not True:
            found.append("the interface does not use dhcp")
        want_vip = {"ip": vip} if role == "controlplane" else _MISSING
        if nic.get("vip", _MISSING) != want_vip:
            found.append("the interface's vip does not match the node's role")
    for _api, kind, doc_name in sorted(final_docs - base_docs, key=str):
        found.append(f"a patch added a {kind or 'untyped'} document"
                     + (f" ({doc_name})" if doc_name else ""))
    return found


def main(argv):
    if len(argv) != 7:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    found = problems(*argv[1:])
    for problem in found:
        print(f"{argv[3]}: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
