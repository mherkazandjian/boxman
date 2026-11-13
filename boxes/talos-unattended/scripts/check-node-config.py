#!/usr/bin/env python3
"""
Check that a merged Talos machine config kept what patches may not change.

    check-node-config.py BASE NODE_PATCH FINAL NAME ROLE MAC VIP

BASE is the role config ``talosctl gen config`` produced from this cluster's
secrets bundle, NODE_PATCH the per-node patch gen-config.sh generated, and
FINAL the config after the site's patches and then NODE_PATCH were applied.
Schema validation cannot tell whether a valid patch copied from another
cluster replaced this one's identity (``talosctl validate --strict`` accepts
a renamed cluster or a deleted RBAC authorizer), so gen-config.sh runs this
after each merge and publishes nothing if it fails.

Talos v1.12+ splits the configuration into documents keyed by kind and,
for some kinds, name. FINAL must have:

- exactly the documents of BASE plus those of NODE_PATCH: a patch may change
  documents but neither add one (a second link, VIP or trusted root beside
  the checked ones) nor delete one (``$patch: delete``);
- every PROTECTED_KINDS document exactly as in BASE: cluster identity, the
  Kubernetes CAs and service-account key, the secrets encryption key,
  authentication and authorization, the install inputs and the CIDRs;
- the PROTECTED_PATHS of the main v1alpha1 document as in BASE, including
  staying absent: its own identity (type, tokens, CAs) and the legacy fields
  that would duplicate the documents above;
- every NODE_PATCH document exactly as generated, and in particular the
  node's hostname, its one MAC-selected link on DHCP, and the VIP on that
  link if and only if ROLE is controlplane.

Only the names of violated settings are printed, never their values: the
values include private keys. Exit status 1 on any violation.
"""

from __future__ import annotations

import sys

import yaml

#: documents that must come through every patch unchanged
PROTECTED_KINDS = (
    "DiscoveryIdentityConfig",     # cluster id and secret
    "KubeClusterConfig",           # cluster name and API endpoint
    "KubeNetworkConfig",           # pod and service CIDRs
    "KubeAPIServerCAConfig",       # Kubernetes CA (accepted CA on workers)
    "KubeAggregatorCAConfig",
    "KubeServiceAccountConfig",
    "KubeEtcdEncryptionConfig",    # secrets encryption key
    "KubeAuthenticationConfig",
    "KubeAuthorizerConfig",
    "UnattendedInstallConfig",     # installer image and install disk
)

#: v1alpha1 paths that must come through every patch unchanged (or absent)
PROTECTED_PATHS = (
    "machine.type",
    "machine.token",
    "machine.ca",
    "machine.acceptedCAs",
    "machine.install",
    "machine.network",
    "machine.time",
    "cluster.id",
    "cluster.secret",
    "cluster.clusterName",
    "cluster.controlPlane",
    "cluster.token",
    "cluster.ca",
    "cluster.aggregatorCA",
    "cluster.serviceAccount",
    "cluster.secretboxEncryptionSecret",
    "cluster.aescbcEncryptionSecret",
    "cluster.etcd.ca",
    "cluster.network",
    "cluster.discovery",
)

MAIN = ("v1alpha1", None)
_MISSING = object()


def _get(doc, path):
    for key in path.split("."):
        if not isinstance(doc, dict) or key not in doc:
            return _MISSING
        doc = doc[key]
    return doc


def _label(key):
    kind, name = key
    if key == MAIN:
        return "the v1alpha1 document"
    return f"the {kind} document" + (f" ({name})" if name else "")


def _load(path):
    """{(kind, name): document}; the main document is MAIN."""
    docs = {}
    with open(path, encoding="utf-8") as fobj:
        for doc in yaml.safe_load_all(fobj):
            if doc is None:
                continue
            if not isinstance(doc, dict):
                raise SystemExit(f"{path}: a document is not a mapping")
            if "kind" in doc:
                key = (str(doc["kind"]), doc.get("name"))
            elif doc.get("version") == "v1alpha1":
                key = MAIN
            else:
                raise SystemExit(f"{path}: a document has neither a kind nor version v1alpha1")
            if key in docs:
                raise SystemExit(f"{path}: {_label(key)} appears twice")
            docs[key] = doc
    return docs


def _of_kind(docs, kind):
    return [doc for (k, _), doc in docs.items() if k == kind]


def problems(base_path, patch_path, final_path, name, role, mac, vip):
    base, patch, final = _load(base_path), _load(patch_path), _load(final_path)
    found = []
    if MAIN not in base or MAIN not in final:
        return ["the v1alpha1 document is missing"]
    if _get(base[MAIN], "machine.type") != role:
        found.append(f"the generated base is not a {role} config")

    expected = set(base) | set(patch)
    for key in sorted(set(final) - expected, key=str):
        found.append(f"a patch added {_label(key)}")
    for key in sorted(expected - set(final), key=str):
        found.append(f"a patch deleted {_label(key)}")

    for key in sorted(base, key=str):
        if key[0] in PROTECTED_KINDS and key in final and final[key] != base[key]:
            found.append(f"{_label(key)} was changed by a patch")
    for path in PROTECTED_PATHS:
        if _get(base[MAIN], path) != _get(final[MAIN], path):
            found.append(f"{path} was changed by a patch")
    for key in sorted(patch, key=str):
        if key in final and final[key] != patch[key]:
            found.append(f"{_label(key)} differs from the generated node patch")

    # what the generated node patch must say, checked independently of it
    hostname = _of_kind(final, "HostnameConfig")
    if len(hostname) != 1 or hostname[0].get("hostname") != name \
            or hostname[0].get("auto") not in ("off", False):   # YAML 1.1 reads off as false
        found.append("the HostnameConfig is not the node's static name")
    aliases = _of_kind(final, "LinkAliasConfig")
    want_match = f'mac(link.permanent_addr) == "{mac.lower()}"'
    if len(aliases) != 1 or _get(aliases[0], "selector.match") != want_match:
        found.append("the node's link is not selected by exactly its mac")
        link = None
    else:
        link = aliases[0].get("name")
    dhcp = _of_kind(final, "DHCPv4Config")
    if link is None or [d.get("name") for d in dhcp] != [link]:
        found.append("dhcp is not configured on exactly the node's link")
    vips = _of_kind(final, "Layer2VIPConfig")
    want_vips = [(vip, link)] if role == "controlplane" else []
    if [(v.get("name"), v.get("link")) for v in vips] != want_vips:
        found.append("the vip does not match the node's role")
    return found


def main(argv):
    if len(argv) != 8:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    found = problems(*argv[1:])
    for problem in found:
        print(f"{argv[4]}: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
