"""
Fake host and cluster commands for tests/test_talos_unattended_driver.py.

One script stands in for virsh, ip, df, boxman, ssh, talosctl, kubectl, flux
and curl: ``python talos_unattended_fakes.py <tool> ARGS...``. They share one
JSON "world" (``FAKE_WORLD``) that the test sets up and inspects, and append
every call to ``FAKE_LOG`` as ``<tool> <args>``. Nothing here touches libvirt,
the network or a real cluster.

Failure injection, all in the world:

- ``fail``: a list of entries like ``"virsh net-dumpxml"``, ``"ip route"``
  or ``"talosctl bootstrap"``: the tool, then words that must appear in its
  arguments in that order. A matching call exits 1 and changes nothing.
- ``garbage``: entry -> text printed instead of the real answer (rc 0).
- the talos, kube and flux sections hold the state the cluster commands read
  and write; see the handlers below.

talosctl's config-generation subcommands (gen, machineconfig, validate,
config, version --client) are delegated to the fake of
tests/test_talos_unattended_box.py (``FAKE_TALOSCTL_GEN``), so the real
gen-config.sh runs unchanged against it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys

WORLD = os.environ.get("FAKE_WORLD", "")


def load() -> dict:
    with open(WORLD) as fobj:
        return json.load(fobj)


def save(world: dict) -> None:
    tmp = WORLD + ".tmp"
    with open(tmp, "w") as fobj:
        json.dump(world, fobj, indent=1, sort_keys=True)
    os.replace(tmp, WORLD)


def log(tool: str, argv: list[str]) -> None:
    with open(os.environ["FAKE_LOG"], "a") as fobj:
        fobj.write(" ".join([tool, *argv]) + "\n")


def fail(msg: str, rc: int = 1):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)


def matches(entry: str, tool: str, argv: list[str]) -> bool:
    """*entry* names *tool* and its other words appear in *argv* in order."""
    want = entry.split()
    if not want or want[0] != tool:
        return False
    rest = iter(argv)
    return all(any(arg == word for arg in rest) for word in want[1:])


def injected(world: dict, tool: str, argv: list[str]) -> None:
    for entry in world.get("fail", []):
        if matches(entry, tool, argv):
            fail(f"fake: injected failure of {entry!r}")
    for entry, text in (world.get("garbage") or {}).items():
        if matches(entry, tool, argv):
            sys.stdout.write(text)
            sys.exit(0)


def opt(argv: list[str], flag: str) -> str | None:
    for i, arg in enumerate(argv[:-1]):
        if arg == flag:
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    for arg in argv:
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


# --------------------------------------------------------------------------- #
# host: virsh, ip, df, boxman                                                 #
# --------------------------------------------------------------------------- #
def net_xml(name: str, net: dict) -> str:
    if "xml" in net:
        return net["xml"]
    return (f"<network><name>{name}</name><bridge name='{net.get('bridge', 'virbr99')}'/>"
            f"<ip address='{net['address']}' netmask='{net.get('netmask', '255.255.255.0')}'/>"
            "</network>")


def virsh(world: dict, argv: list[str]) -> None:
    if argv[:1] == ["-c"]:
        argv = argv[2:]
    cmd, args = argv[0], argv[1:]
    doms = world.setdefault("domains", {})
    nets = world.setdefault("networks", {})
    if cmd == "list":
        print("\n".join([*doms, *world.get("other_domains", [])]))
    elif cmd == "net-list":
        print("\n".join(nets))
    elif cmd == "net-dumpxml":
        if args[0] not in nets:
            fail(f"error: failed to get network '{args[0]}'")
        print(net_xml(args[0], nets[args[0]]))
    elif cmd == "net-info":
        if args[0] not in nets:
            fail(f"error: failed to get network '{args[0]}'")
        print(f"Name: {args[0]}")
    elif cmd == "net-uuid":
        if args[0] not in nets:
            fail(f"error: failed to get network '{args[0]}'")
        print(nets[args[0]].get("uuid", "net-uuid-1"))
    elif cmd in ("domstate", "domuuid", "domiflist", "domblklist", "domblkinfo",
                 "qemu-agent-command"):
        dom = doms.get(args[0])
        if dom is None:
            fail(f"error: failed to get domain '{args[0]}'")
        if cmd == "domstate":
            print(dom["state"])
        elif cmd == "domuuid":
            print(dom["uuid"])
        elif cmd == "domiflist":
            print(" Interface   Type      Source   Model    MAC\n" + "-" * 40)
            for nic in dom.get("nics", []):
                print(f" vnet0   network   {nic['source']}   virtio   {nic['mac']}")
        elif cmd == "domblklist":
            print(" Type   Device   Target   Source\n" + "-" * 40)
            for disk in dom.get("disks", []):
                print(f" file   {disk['device']}   {disk['target']}   {disk['source']}")
        elif cmd == "domblkinfo":
            disk = next(d for d in dom.get("disks", []) if d["target"] == args[1])
            print(f"Capacity:       {disk['capacity']}\nAllocation:     {disk.get('allocation', 200704)}\n"
                  f"Physical:       {disk.get('allocation', 200704)}")
        else:
            agent(dom, json.loads(args[1]))
            save(world)
    elif cmd == "net-dhcp-leases":
        mac = opt(args, "--mac")
        print(" Expiry Time           MAC address         Protocol   IP address    Hostname   Client ID\n"
              + "-" * 60)
        for lease in world.get("leases", {}).get(args[0], []):
            if mac is None or lease["mac"] == mac:
                print(f" 2026-01-01 00:00:00   {lease['mac']}   ipv4   {lease['ip']}/24   -   -")
    else:
        fail("fake virsh: unexpected " + " ".join(argv))


def agent(dom: dict, request: dict) -> None:
    files = (dom.get("agent") or {}).get("files")
    if files is None:
        fail("error: Guest agent is not responding: QEMU guest agent is not connected")
    name, args = request["execute"], request.get("arguments", {})
    if name == "guest-file-open":
        if args["path"] not in files:
            fail("error: internal error: unable to execute QEMU agent command: No such file")
        dom.setdefault("open", {})["1000"] = args["path"]
        print(json.dumps({"return": 1000}))
    elif name == "guest-file-read":
        data = files[dom["open"][str(args["handle"])]].encode()
        print(json.dumps({"return": {"count": len(data), "eof": True,
                                     "buf-b64": base64.b64encode(data).decode()}}))
    elif name == "guest-file-close":
        print(json.dumps({"return": {}}))
    else:
        fail("fake agent: unexpected " + name)


def ip(world: dict, argv: list[str]) -> None:
    if argv[:3] == ["-4", "-o", "addr"]:
        print("\n".join(world.get("host_addrs", [])))
    elif argv[:2] == ["-4", "route"]:
        print("\n".join(world.get("host_routes", [])))
    else:
        fail("fake ip: unexpected " + " ".join(argv))


def df(world: dict, argv: list[str]) -> None:
    print("Filesystem     1024-blocks      Used Available Capacity Mounted on")
    print(f"/dev/fake      999999999999 1 {world.get('df_avail_kib', 10 ** 12)} 1% /")


def boxman(world: dict, argv: list[str]) -> None:
    if "up" not in argv:
        fail("fake boxman: unexpected " + " ".join(argv))
    up = world.setdefault("boxman", {})
    up["calls"] = up.get("calls", 0) + 1
    if up.get("rc"):
        fail("fake boxman: up failed", up["rc"])
    for name, dom in (up.get("creates") or {}).items():
        world["domains"].setdefault(name, json.loads(json.dumps(dom)))
        world["domains"][name]["state"] = "running"
    for name, net in (up.get("networks") or {}).items():
        world["networks"].setdefault(name, net)
    for net, leases in (up.get("leases") or {}).items():
        world.setdefault("leases", {})[net] = leases
    save(world)
    if up.get("fail_after_create"):
        fail("fake boxman: up failed after creating the domains", 1)


# --------------------------------------------------------------------------- #
# ssh to the fake control VM: runs the command locally                       #
# --------------------------------------------------------------------------- #
def ssh(world: dict, argv: list[str]) -> None:
    opts, rest, i = {}, [], 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-F", "-i", "-p", "-l"):
            opts[arg] = argv[i + 1]
            i += 2
        elif arg == "-o":
            key, _, value = argv[i + 1].partition("=")
            opts[key] = value
            i += 2
        elif arg.startswith("-") and not rest:
            i += 1
        else:
            rest.append(arg)
            i += 1
    control = world["control"]
    dest, command = rest[0], " ".join(rest[1:])
    if control.get("ssh_down"):
        fail(f"ssh: connect to host {dest}: Connection refused", 255)
    if dest != f"{control['user']}@{control['ip']}":
        fail(f"ssh: connect to host {dest}: No route to host", 255)
    if opts.get("StrictHostKeyChecking") != "yes" or opts.get("BatchMode") != "yes":
        fail("fake ssh: refusing a connection without strict host key checking", 255)
    alias, known = opts.get("HostKeyAlias", dest), opts.get("UserKnownHostsFile", "")
    want = f"{alias} {control['host_key']}"
    if not known or not os.path.exists(known) or want not in open(known).read().splitlines():
        fail("Host key verification failed.", 255)
    if opts.get("IdentityFile") != control["key_path"]:
        fail(f"{dest}: Permission denied (publickey).", 255)
    # a real ssh session inherits nothing from the host: only the harness's
    # plumbing (fake world, log, PATH) reaches the "remote" command, so every
    # setting the control VM needs must arrive in the shipped inputs
    env = {k: v for k, v in os.environ.items()
           if k.startswith("FAKE_") or k in ("PATH", "PYTHONPATH", "LANG", "LC_ALL", "TMPDIR")}
    env["HOME"] = control["home"]
    os.makedirs(control["home"], exist_ok=True)
    rc = subprocess.run(["bash", "-c", command], cwd=control["home"], env=env).returncode
    sys.exit(rc)


# --------------------------------------------------------------------------- #
# talosctl cluster operations                                                 #
# --------------------------------------------------------------------------- #
GEN = {("version", "--client"), ("gen", "secrets"), ("gen", "config"),
       ("machineconfig", "patch"), ("validate", "--config"), ("config", "endpoint"),
       ("config", "node")}


def talos_words(argv: list[str]) -> tuple[dict, list[str]]:
    flags, words, i = {"insecure": False}, [], 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--talosconfig", "-e", "--endpoints", "-n", "--nodes", "-o", "--file",
                   "--timeout"):
            flags[arg.lstrip("-")[0] if arg in ("-e", "-n", "-o") else arg.lstrip("-")] = argv[i + 1]
            i += 2
        elif arg == "--insecure":
            flags["insecure"] = True
            i += 1
        else:
            words.append(arg)
            i += 1
    return flags, words


def dump_yaml(node_ip: str, kind: str, items: list[dict]) -> None:
    import yaml
    docs = [{"node": node_ip, "metadata": {"namespace": "runtime", "type": kind, "id": it["id"]},
             "spec": {k: v for k, v in it.items() if k != "id"}} for it in items]
    sys.stdout.write(yaml.safe_dump_all(docs, sort_keys=False))


def hang_if(entries, words) -> None:
    """Sleep "forever" when an entry's words all appear in *words*."""
    import time
    for entry in entries or []:
        if all(w in words for w in entry.split()):
            time.sleep(3600)


def talosctl(world: dict, argv: list[str]) -> None:
    flags, words = talos_words(argv)
    hang_if(world.get("talos", {}).get("hang"), words)
    if tuple(words[:2]) in GEN:
        gen = os.environ["FAKE_TALOSCTL_GEN"]
        os.execv(gen, [gen, *argv])
    talos = world.setdefault("talos", {})
    nodes = talos.setdefault("nodes", {})
    addr = flags.get("n")
    if addr is None:
        fail("fake talosctl: every cluster call must name its node")
    if flags.get("e") not in (None, addr):
        fail("fake talosctl: endpoint and node differ")
    if addr == talos.get("vip"):
        fail("fake talosctl: refusing a Talos API call through the VIP")
    node = nodes.get(addr)
    if node is None or node.get("mode") == "off":
        fail(f"rpc error: code = Unavailable desc = connection error: dial tcp {addr}:50000: "
             "connect: no route to host")
    cmd = " ".join(words)
    if flags["insecure"]:
        if node["mode"] != "maintenance":
            fail("rpc error: code = Unavailable desc = connection error: tls: certificate required")
        if words[0] == "get":
            kind = words[1]
            if kind == "discoveredvolumes" and node.get("volumes_forbidden"):
                fail("rpc error: code = PermissionDenied desc = not allowed in maintenance mode")
            items = {"disks": node.get("disks", []), "links": node.get("links", []),
                     "discoveredvolumes": node.get("volumes", [])}.get(kind)
            if items is None:
                fail("fake talosctl: unexpected insecure get " + kind)
            dump_yaml(addr, kind, items)
        elif words[0] == "apply-config":
            with open(flags["file"], "rb") as fobj:
                sha = hashlib.sha256(fobj.read()).hexdigest()
            mode = (talos.get("apply_fail") or {}).get(addr)
            node["applies"] = node.get("applies", 0) + 1
            if mode == "error":
                save(world)
                fail("rpc error: code = Unavailable desc = injected apply failure")
            node.update(mode="installed", applied=sha)
            save(world)
            if mode == "lost":
                fail("rpc error: code = Unavailable desc = connection reset (response lost)")
        else:
            fail("fake talosctl: unexpected insecure " + cmd)
        return
    # authenticated
    tc = flags.get("talosconfig")
    if not tc or not os.path.exists(tc):
        fail("fake talosctl: authenticated call without the cluster's talosconfig")
    if node["mode"] != "installed" or node.get("auth") == "reject":
        fail("rpc error: code = Unavailable desc = connection error: tls: "
             "failed to verify certificate: x509: certificate signed by unknown authority")
    etcd = talos.setdefault("etcd_members", [])
    if words == ["version"]:
        print(f"Client:\n\tTag: {os.environ.get('FAKE_TALOS_VERSION')}\nServer:\n\tNODE: {addr}")
    elif words[:2] == ["get", "timestatus"]:
        dump_yaml(addr, "TimeStatuses.v1alpha1.talos.dev",
                  [{"id": "node", "synced": node.get("synced", True), "epoch": 0, "syncDisabled": False}])
    elif words[:2] == ["get", "links"]:
        dump_yaml(addr, "LinkStatuses.net.talos.dev", node.get("links", []))
    elif words == ["service", "etcd"]:
        state = node.get("etcd_state")
        if state is None:
            if etcd and not talos.get("etcd_down"):
                state = "Running"
            elif talos.get("bootstrapped"):
                state = "Failed"
            else:
                state = "Preparing"
        print(f"NODE     {addr}\nID       etcd\nSTATE    {state}\nHEALTH   ?\n"
              "EVENTS   [Preparing]: Running pre state (1m ago)")
    elif words == ["list", "/var/lib/etcd"]:
        mode = node.get("etcd_dir")
        if mode == "missing":
            fail("rpc error: code = Unknown desc = lstat /var/lib/etcd: no such file or directory")
        if mode == "error":
            fail("rpc error: code = PermissionDenied desc = not allowed")
        if mode == "garbage":
            print("something unexpected")
            return
        entries = ["."]
        if mode == "data" or (talos.get("bootstrapped") and node.get("role") == "controlplane"):
            entries.append("member")
        # one node: names only, no header (talosctl v1.14, seen live); several
        # nodes: a NODE/NAME header and two columns
        if node.get("list_format") == "multi":
            print("NODE   NAME")
            for entry in entries:
                print(f"{addr}   {entry}")
        else:
            print("\n".join(entries))
    elif words == ["bootstrap"]:
        talos["bootstrap_calls"] = talos.get("bootstrap_calls", 0) + 1
        mode = talos.get("bootstrap_fail")
        if etcd:
            save(world)
            fail("rpc error: code = AlreadyExists desc = etcd data directory is not empty")
        if mode == "error-before":
            save(world)
            fail("rpc error: code = Unavailable desc = injected bootstrap failure")
        talos["etcd_members"] = [n["name"] for n in nodes.values()
                                 if n.get("role") == "controlplane" and n["mode"] == "installed"]
        talos["bootstrapped"] = True
        save(world)
        if mode == "error-after":
            fail("rpc error: code = Unavailable desc = connection reset (response lost)")
    elif words[:2] == ["etcd", "members"]:
        if not etcd or talos.get("etcd_down"):
            fail("rpc error: code = Unavailable desc = etcd is not running")
        print("NODE   ID   HOSTNAME   PEER URLS   CLIENT URLS   LEARNER")
        for i, name in enumerate(etcd):
            print(f"{addr}   {i + 1:016x}   {name}   https://x:2380   https://x:2379   false")
    elif words[0] == "kubeconfig":
        if not etcd:
            fail("rpc error: code = Unavailable desc = kubernetes is not up")
        path = words[1]
        if os.path.exists(path):
            fail("fake talosctl: kubeconfig would merge into an existing file")
        with open(path, "w") as fobj:
            fobj.write(f"apiVersion: v1\nkind: Config\nserver: https://{talos.get('vip')}:6443\n"
                       "client-key-data: FAKESECRET-kube\n")
        talos["kubeconfig_calls"] = talos.get("kubeconfig_calls", 0) + 1
        save(world)
    else:
        fail("fake talosctl: unexpected " + cmd)


# --------------------------------------------------------------------------- #
# kubectl, flux, curl                                                         #
# --------------------------------------------------------------------------- #
def kubectl(world: dict, argv: list[str]) -> None:
    kc = opt(argv, "--kubeconfig")
    if argv[:2] == ["version", "--client"] or argv[1:3] == ["version", "--client"]:
        print(json.dumps({"clientVersion": {"gitVersion": world.get("kubectl_version", "v1.99.0")}}))
        return
    if not kc or not os.path.exists(kc) or "FAKESECRET-kube" not in open(kc).read():
        fail("error: no usable kubeconfig")
    talos, kube = world.get("talos", {}), world.setdefault("kube", {})
    hang_if(kube.get("hang"), argv)
    up = bool(talos.get("etcd_members")) and not kube.get("api_down")
    if not up:
        fail("Unable to connect to the server: dial tcp: connect: connection refused")
    nodes = [n for n in talos.get("nodes", {}).values() if n["mode"] == "installed"]
    words = [a for a in argv if not a.startswith("--") and a != kc]
    ns = opt(argv, "-n") or opt(argv, "--namespace")
    words = [w for w in words if w not in ("-n", ns)]
    if "--raw=/readyz" in argv:
        print("ok")
    elif words[:2] == ["get", "nodes"]:
        for n in nodes:
            ready = "NotReady" if n["name"] in kube.get("not_ready", []) else "Ready"
            print(f"{n['name']}   {ready}   <none>   1m   v1.99.0")
    elif words[:1] == ["wait"] and "nodes" in words:
        if any(n["name"] in kube.get("not_ready", []) for n in nodes):
            fail("error: timed out waiting for the condition on nodes/" + kube["not_ready"][0])
        print("node/x condition met")
    elif words[:2] == ["rollout", "status"]:
        target = words[2]
        if target in kube.get("not_rolled_out", []):
            fail(f"error: timed out waiting for the rollout of {target}")
        print(f'{target} successfully rolled out')
    elif words[:1] == ["apply"]:
        kube.setdefault("applied", []).append(sys.stdin.read())
        save(world)
        print("deployment.apps/smoke configured")
    elif words[:2] == ["get", "pods"]:
        if kube.get("smoke_pending"):
            print("smoke-1 Pending ")
        else:
            workers = [n["name"] for n in nodes if n.get("role") == "worker"] or ["?"]
            print(f"smoke-1 Running {workers[0]}")
    elif words[:2] == ["get", "crd"]:
        if world.get("flux", {}).get("installed") or world.get("flux", {}).get("crd"):
            print("customresourcedefinition.apiextensions.k8s.io/" + words[2])
    elif words[:2] == ["get", "gitrepository"] or words[:2] == ["get", "kustomization"]:
        flux = world.get("flux", {}).get("installed")
        if not flux and "--ignore-not-found" in argv:
            return
        if not flux:
            fail(f'Error from server (NotFound): {words[1]}s "flux-system" not found')
        if words[1] == "gitrepository":
            print(f"{flux['url']} {flux['branch']}")
        else:
            print(flux["path"])
    elif words[:1] == ["wait"]:
        flux = world.get("flux", {})
        if not flux.get("installed") or flux.get("not_ready"):
            fail("error: timed out waiting for the condition on gitrepositories/flux-system")
        print("condition met")
    else:
        fail("fake kubectl: unexpected " + " ".join(argv))


def flux(world: dict, argv: list[str]) -> None:
    state = world.setdefault("flux", {})
    if argv[:2] == ["version", "--client"]:
        print("flux: " + world.get("flux_version", "v2.99.0"))
    elif argv[:2] == ["bootstrap", "gitlab"]:
        state["bootstrap_calls"] = state.get("bootstrap_calls", 0) + 1
        save(world)
        if os.environ.get("GITLAB_TOKEN") != state.get("token"):
            fail("✗ failed to authenticate with GitLab: 401 Unauthorized")
        host, owner = opt(argv, "--hostname"), opt(argv, "--owner")
        path = opt(argv, "--path").removeprefix("./").rstrip("/")
        if state.get("bootstrap_fail"):
            if state["bootstrap_fail"] == "after-push":
                world.setdefault("gitlab", {}).setdefault("paths", []).append(path)
                state["crd"] = True
                save(world)
            fail("✗ injected bootstrap failure")
        world.setdefault("gitlab", {}).setdefault("paths", []).append(path)
        state["installed"] = {"url": f"https://{host}/{owner}/{opt(argv, '--repository')}.git",
                              "branch": opt(argv, "--branch"), "path": opt(argv, "--path")}
        state["args"] = argv
        save(world)
        print("✔ all components are healthy")
    elif argv[:1] == ["check"]:
        if not state.get("installed") or state.get("not_ready"):
            fail("✗ flux-system not ready")
        print("✔ all checks passed")
    else:
        fail("fake flux: unexpected " + " ".join(argv))


def curl(world: dict, argv: list[str]) -> None:
    """The GitLab API: prints the body, a newline and the HTTP status."""
    from urllib.parse import parse_qs, unquote, urlparse
    header = sys.stdin.read().strip() if "@-" in argv else ""
    gitlab = world.get("gitlab", {})
    if gitlab.get("down"):
        fail("curl: (7) Failed to connect", 7)
    url = urlparse(argv[-1])
    token = header.split(":", 1)[1].strip() if ":" in header else ""

    def answer(status: int, body: str = "{}") -> None:
        sys.stdout.write(f"{body}\n{status}")
        sys.exit(0)

    if token != gitlab.get("token"):
        answer(401, '{"message":"401 Unauthorized"}')
    rest = url.path.split("/api/v4/projects/", 1)[1]
    project, _, sub = rest.partition("/")
    project = unquote(project)
    if project not in gitlab.get("projects", []):
        answer(404, '{"message":"404 Project Not Found"}')
    if not sub:
        answer(200, json.dumps({"id": 1, "path_with_namespace": project}))
    if sub.startswith("repository/branches/"):
        branch = unquote(sub.split("repository/branches/", 1)[1])
        answer(200 if branch in gitlab.get("branches", ["main"]) else 404)
    if sub == "repository/tree":
        if gitlab.get("tree_status"):
            answer(gitlab["tree_status"], '{"message":"error"}')
        path = parse_qs(url.query)["path"][0]
        if path in gitlab.get("paths", []):
            answer(200, '[{"name":"flux-system","type":"tree"}]')
        answer(404, '{"message":"404 Tree Not Found"}')
    fail("fake curl: unexpected " + argv[-1])


TOOLS = {"virsh": virsh, "ip": ip, "df": df, "boxman": boxman, "ssh": ssh,
         "talosctl": talosctl, "kubectl": kubectl, "flux": flux, "curl": curl}


def main() -> None:
    tool, argv = sys.argv[1], sys.argv[2:]
    log(tool, argv)
    world = load()
    injected(world, tool, argv)
    TOOLS[tool](world, argv)


if __name__ == "__main__":
    main()
