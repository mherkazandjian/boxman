# dc-provider-staging

The **staging/testing environment** for the docker-compose provider epic
([#42](https://github.com/mherkazandjian/boxman/issues/42)) — the de facto
place to test every change across the epic's phases. A single Ubuntu 24.04 VM
(`stage01`) pre-baked with:

| capability | why |
|---|---|
| docker + compose v2 | provider lifecycle work (phases 3–7), netfilter spike ([#48](https://github.com/mherkazandjian/boxman/issues/48)) |
| libvirt/qemu-kvm + virtinst (nested — template built with `--cpu host-passthrough`) | hybrid VM↔container e2e (phases 4/8): boxman *inside* the VM provisions nested VMs next to containers |
| git, make, python3 + pip/venv | clone boxman, install it, run the test suite inside |
| `br_netfilter` loaded at boot | spike scenarios manipulate `bridge-nf-call-iptables` |

Being a VM, it's **disposable and snapshot-resettable**: destructive tests
(docker restarts, iptables surgery, sysctl flips) never touch your host.

## Prerequisites (host)

- libvirt/KVM working without sudo (`virsh -c qemu:///system list`)
- boxman on python ≥ 3.10. No system python 3.10+? User-space fix:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  uv python install 3.12 && uv venv ~/.venvs/boxman --python 3.12
  VIRTUAL_ENV=~/.venvs/boxman uv pip install -e <path-to-boxman-repo>
  export PATH=~/.venvs/boxman/bin:$PATH
  ```

## Bring it up

```bash
cd boxes/dc-provider-staging
boxman up            # first run: downloads the cloud image, builds the
                     # template (installs docker+libvirt inside — takes a
                     # few minutes), clones + starts stage01
```

## Bootstrap (once per fresh VM)

```bash
SSH="ssh -F ~/workspaces/boxmandev/dc-provider-staging/staging/ssh_config stage01"
$SSH 'git clone https://github.com/mherkazandjian/boxman.git ~/boxman'
$SSH 'cd ~/boxman && python3 -m venv ~/.venv-boxman && ~/.venv-boxman/bin/pip install -e . pytest'
$SSH 'cd ~/boxman && PYTHONPATH=src ~/.venv-boxman/bin/python -m pytest tests/ -m unit -q'   # sanity
```

*(the ssh_config path is printed by `boxman up`; adjust if your workspace
differs)*

## Sanity checks inside

```bash
$SSH 'docker run --rm hello-world | tail -2'     # docker works
$SSH 'egrep -c "vmx|svm" /proc/cpuinfo'          # nested virt exposed (>0)
$SSH 'lsmod | grep br_netfilter'                 # netfilter module loaded
```

## Run the Phase 0 netfilter spike

```bash
$SSH 'cd ~/boxman && git fetch origin dc-provider/phase-0-design-closure && git checkout dc-provider/phase-0-design-closure'
$SSH 'cd ~/boxman && sudo bash doc/docker-compose-provider/spike/poc.sh --with-docker-restart --emulate-docker-policy'
```

Paste the output into `doc/docker-compose-provider/spike/findings.md`.

## Snapshot workflow (the point of this box)

```bash
# after bootstrap, freeze the known-good state:
boxman snapshot take pristine

# ...hack, test, break things inside...

# reset to known-good between destructive runs:
boxman snapshot restore pristine
```

## Teardown

```bash
boxman destroy
```

The base template + downloaded image stay cached for the next `boxman up`.
