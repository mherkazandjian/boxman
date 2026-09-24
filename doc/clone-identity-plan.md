# Clone identity: one offline pass for machine-id, hostname and SSH host keys

Design for the shared spine behind three issues:

- **#200** — a clone boots under its template's hostname; set it from the vm's `hostname:`.
- **#201** — every clone presents its template's SSH host keys.
- **#202** — a clone that keeps its template's identity under `auto` should be impossible to miss.

All three extend the same thing: the single offline `virt-sysprep` call that
`CloneVM` already runs against a shut-off clone. This document specifies that
spine once, so the three do not each invent their own answer. It ships with
PR 1 (spine + #201).

## 1. Where we are today

`CloneVM.create_clone()` runs `virt-clone`, then `apply_machine_identity_policy()`,
which calls `reset_machine_identity()`:

```
virt-sysprep --connect <uri> --domain <clone> --operations machine-id --keys-from-stdin
```

One property (the machine ID), one policy key (`clone_machine_id: auto|required|off`),
one invocation. Failures are typed (`CloneSanitizerError` and subclasses); `auto`
appends a message to `info[CLONE_DEGRADATION_NOTICES_KEY]` and continues,
`required` calls `discard_unsafe_clone()` and re-raises, `off` skips the call.
`manager_parts/vms.py::_clone_with_retry` re-emits the collected notices after
log suppression ends.

## 2. What `virt-sysprep` actually does — measured, not assumed

Everything in this section was verified by execution against **guestfs-tools
1.52.2**, the version in `containers/docker/Dockerfile`, using the bundled
`boxman-ready-boxman-libvirt` image with `LIBGUESTFS_BACKEND=direct` and a
synthetic inspectable ext4 root (`/etc/os-release`, `/etc/fstab`, `/etc`, `/bin`).
It matters because **two of the mechanisms issue #200 suggests do not exist**,
and one failure mode is silent.

| # | Finding | Consequence |
|---|---|---|
| F1 | There is **no `hostname` sysprep operation**. The list has `net-hostname` ("Remove HOSTNAME and DHCP_HOSTNAME in network interface configuration"), which is a *removal*, not a set. | #200's suggested `--operations machine-id,hostname` fails outright: `--operations: 'hostname' is not a known operation`. |
| F2 | `--hostname` is a **customization option**, applied by the `customize` *operation*. `--operations` **replaces** the default set, and boxman passes `--operations machine-id`, so `customize` is not enabled. `virt-sysprep -a disk --operations machine-id --hostname X` **exits 0, prints nothing unusual, and does not set the hostname.** | The central trap. Any customization added without also enabling `customize` is a silent no-op, and a unit test that only asserts the built command string would pass. |
| F3 | Operation order is fixed and `customize` runs **last**. Observed `ssh-hostkeys` → `customize`, and `machine-id` → `customize`; both are the reverse of alphabetical, so this is deliberate ordering, not luck. A `--write` to a path that `ssh-hostkeys` had just deleted survived. | Delete-then-replace is safe **in a single pass**: `ssh-hostkeys` removes the inherited keys, then `customize` uploads fresh ones. |
| F4 | The `customize` operation **always writes a populated random `/etc/machine-id`** — even with no customization flags at all ("Setting a random seed", "Setting the machine ID in /etc/machine-id"). `--operations machine-id` alone **truncates** it to empty. | Enabling `customize` changes today's machine-id behaviour. See §4. |
| F5 | *Revised during #200.* `--hostname` writes `/etc/hostname` as given (an FQDN included) and the distro files, and its `/etc/hosts` handling is **distro-specific**: on a RedHat root it leaves `/etc/hosts` alone; on a Debian root it replaces the old *short* name with the new value and leaves the old FQDN behind (`127.0.1.1 hello-cloudinit.localdomain node01.example.com`). The original measurement used a Fedora-shaped root, which hid the Debian behaviour. | #200 §3 needs its own `/etc/hosts` handling on every distro, and it must run *before* `--hostname`, which erases the old name it would otherwise have to find. |
| F6 | `--run-command` requires host/guest arch compatibility and refuses otherwise: *"host cpu (x86_64) and guest arch (unknown) are not compatible … Use --firstboot scripts instead."* | #201's suggested in-appliance `ssh-keygen -A` is not portable. See §5.1. |
| F7 | **No flag suppresses** customize's machine-id or random-seed write. | The §4 interaction cannot be engineered away. |
| F8 | SELinux relabelling is automatic in 1.52; `--selinux-relabel` is documented as "Compatibility option doing nothing", and `--no-selinux-relabel` disables it. | Production invocations must **not** pass `--no-selinux-relabel`, or uploaded host keys get wrong labels on EL guests. |
| F9 | Customizations available: `--upload`, `--write`, `--edit`, `--append-line`, `--delete`, `--chmod`, `--chown`, `--mkdir`, `--touch`, `--copy-in`, `--link`, `--move`, `--run`, `--run-command`, `--firstboot*`. | Enough to do all of #200 and #201 offline in one pass. |
| F10 | `--upload` **preserves the source file's ownership**. An uploaded host key landed in the guest owned by uid 10000 — the hypervisor user's uid — not root. | Ownership has to be set explicitly; uploading alone is not enough. |
| F11 | **`--chown` is broken in guestfs-tools 1.52.0** — the version on the test-runner VM. It documents `--chown UID:GID:PATH`, but its parser rejects *every* form, including its own documented one: `invalid format for '--chown' parameter`. 1.52.2 accepts the identical argument. | `--chown` cannot be relied on. Ownership is carried by a tar archive instead (F12), which also keeps private keys off the command line. |
| F13 | `--edit` evaluates its Perl expression with **`perl` on the host running virt-sysprep**, not in the guest. The bundled docker-runtime image has no perl: `sh: line 1: perl: command not found`. | `--edit` is unusable under the docker runtime. |
| F14 | `--edit` on a file the guest does not have **aborts the whole pass**: `virt-sysprep: error: /etc/cloud/cloud.cfg does not exist in the guest`. | Nothing can be edited conditionally with `--edit` — every guest without cloud-init would fail the pass. |
| F15 | `--mkdir` creates intermediate directories (`mkdir -p`), and a multi-line `--write` value lands intact. Same on 1.52.0 and 1.52.2. A customize run on a root with no `/etc/machine-id` exits 0. | Closes the §9 `--mkdir` question, and #201 is unaffected by guests without a machine id. |
| F16 | *Found on a real guest.* cloud-init's `manage_etc_hosts: true` composes the self-line from **unrelated sources**: its fqdn from the metadata's `local-hostname` (boxman's template name), its short name from user-data (`hello-cloudinit`) — `127.0.1.1 ubuntu-24.04-minimal-base-template-cloudinit hello-cloudinit`. Only one of them matches `/etc/hostname`. | A name-matching rewrite leaves the template's name behind; the whole self-line has to be treated as the template's. |
| F12 | `--tar-in <archive>:<dir>` honours the **uid, gid and mode recorded in the tar entries**, not the source files' — verified on 1.52.0 with source files owned by uid 1001 landing as `uid 0 / gid 0`, modes `0600` / `0644`, with SELinux relabelling still applied. | The portable way to install root-owned files, on both versions. |

Reproduction scripts are not checked in; they build a throwaway disk in a
scratch directory and run the matrix above. The integration tier re-verifies
the same claims against real templates, where `--run-command` and the distro
hostname files behave differently from a synthetic root.

## 3. The spine

### 3.1 Identity properties

Replace the single hard-coded operation with a declared table of *identity
properties*. Each has a name, a config key, the sysprep operations it
contributes, and the customizations it contributes:

| Property | Config key | Operations | Customizations |
|---|---|---|---|
| machine id | `clone_machine_id` | `machine-id` | — |
| hostname | `clone_hostname` | — | `--hostname`, `/etc/hosts` edit, cloud-init drop-in |
| ssh host keys | `clone_ssh_host_keys` | `ssh-hostkeys` | `--tar-in` of one root-owned archive holding every key (§5.1) |

Each key takes `auto | required | off`, default `auto`, with exactly the
semantics `clone_machine_id` has today. Three sibling keys rather than one
umbrella key, because #200 names `clone_hostname` explicitly; a unifying key
can be added later without breaking these.

### 3.2 Building the pass

1. Resolve and validate each property's policy (`ConfigError` on anything else).
2. Build a plan from the properties whose policy is not `off`.
3. If the plan is empty, skip the invocation entirely and log once — this is
   today's `clone_machine_id: off` behaviour, preserved.
4. Otherwise emit **one** invocation: `--operations <union of contributed ops>`,
   plus `customize` **if and only if** the plan contributes customizations,
   followed by the customization flags in the order the plan declares them.
5. Run it bounded by `virt_sysprep_timeout`, as today.

One pass, not three: each invocation boots a libguestfs appliance, and cloning
is already the slow step. F3 proves one pass is sufficient for the delete-then-
replace case.

### 3.3 The `customize` invariant

Because F2 is silent, the builder must enforce, as an assertion rather than a
convention:

> if the plan contributes any customization flag, `customize` is in the
> operation list; and `--hostname` is never emitted without a resolved value.

A unit test asserts the invariant directly, not just the rendered string —
a string assertion is exactly what F2 defeats.

### 3.4 Failure policy

The pass covers several properties with independently configured policies, so
a single failure resolves against the **strictest policy among the enabled
properties**: if any enabled property is `required`, the failure is terminal —
discard the clone and raise, as today. Otherwise every enabled property is
`auto`, and the failure produces degradation notices and continues.

Notices name the **property**, not just the VM, because #202 needs to print
"kept its template's machine-id and host keys" rather than one opaque line.
This is the one spine change #202 depends on.

**Partial application is possible and must be documented.** F6 showed a run
where the hostname was applied and a later customization then failed; the
invocation is not atomic. Under `required` the clone is discarded, so this is
invisible. Under `auto` the clone may have *some* properties applied — the
notice must therefore say that the guest may have kept its template identity,
not that it definitely did.

## 4. Consequence: machine-id semantics change

F4 + F7 mean that as soon as any customization is needed (hostname or host
keys, i.e. the default configuration once #200 and #201 land), `/etc/machine-id`
holds a **fresh random value** instead of being **truncated to empty**.

This is a real change and should be a deliberate one. Recommendation: **accept
it and document it**, because it is strictly better for the property's purpose:

- Every clone gets a distinct machine ID offline, with no dependence on the
  guest regenerating one at boot. That removes the caveat currently in
  `doc/tutorial/README.md` ("Guests that do not regenerate empty machine-ID
  files during boot should use `off`") — the documented reason for choosing
  `off` disappears.
- The integration tier can then assert something stronger than today: two
  clones of one template have **different** machine IDs, rather than merely
  empty ones.

The cost is smaller than first written here. systemd's first-boot rule
(`machine-id(5)`, "First Boot Semantics") treats a **missing**
`/etc/machine-id`, or one containing `uninitialized`, as a first boot — but an
existing **empty** file as *not* one. The old truncation to empty therefore
never produced a first boot either, and writing a populated id changes
`ConditionFirstBoot` only for a template that ships with no `/etc/machine-id`
at all. (Corrected after Codex's review of PR #203; the earlier text claimed
an empty file triggered first boot.) No consumer of `ConditionFirstBoot` or of
an empty machine id exists in boxman or its shipped templates.

### The `clone_machine_id: off` interaction

If `clone_machine_id: off` but `clone_hostname` or `clone_ssh_host_keys` is
enabled, the pass still runs, `customize` still writes a machine ID, and the
explicit `off` is violated. F7 says this cannot be switched off.

**Do not turn this into a hard error.** With `clone_hostname` defaulting to
`auto`, a `ConfigError` here would break every existing config that sets
`clone_machine_id: off` the moment they upgrade. Instead: honour the other
properties, warn once naming the interaction, and document the precedence.
The warning is low-stakes precisely because of the reasoning above — the
documented motivation for `off` is that an *empty* machine ID breaks the
guest, and a valid random one does not.

Rejected: reading the template's machine ID first and writing it back. It
costs a second appliance boot to preserve a value whose whole point was to be
left alone.

## 5. Per-issue mechanisms

### 5.1 #201 — SSH host keys (PR 1, with the spine)

- **Delete**: the `ssh-hostkeys` operation, which removes `/etc/ssh/ssh_host_*`.
- **Replace**: generate the keys **on the host** with `ssh-keygen -q -t <type>
  -N '' -C '' -f <tmp>` into a per-clone temporary directory, pack them into a
  tar archive whose entries carry `uid 0`, `gid 0` and modes `0600` / `0644`,
  and install it with a single `--tar-in <archive>:/etc/ssh`. F3 guarantees it
  lands after the deletion.
- The archive, rather than `--upload` + `--chown` + `--chmod`, because of F10
  and F11: uploads keep the source file's uid, and `--chown` is unusable on
  1.52.0. It also collapses 18 arguments into 2 and keeps the private key
  paths off the command line.
- Do not pass `--no-selinux-relabel` (F8), so EL guests get correct labels.
- Delete the temporary directory even on failure.

Host-side generation is chosen over #201's suggested in-appliance
`ssh-keygen -A` because of F6: `--run-command` needs host/guest arch
compatibility and fails for a cross-arch guest, while uploading files does not
care what the guest's architecture is, whether cloud-init is sealed, or whether
the guest has `ssh-keygen` at all. It is also the only form that works for a
guest whose distro does not regenerate missing keys at boot, which is #201's
stated portability requirement.

To verify in the integration tier: uploaded files' ownership (sshd refuses a
host key not owned by root) and that `sshd` starts on first boot on both an EL
and a Debian/Ubuntu guest.

### 5.2 #200 — hostname (as built)

The plan below replaced an earlier one built on `--edit` and a drop-in alone;
F13, F14 and F16 are why.

- **Resolve**: `hostname:` if declared, otherwise the VM key — the same
  fallback `manager_parts/ssh.py` uses for the ssh alias, so the alias and the
  guest's name agree. The manager resolves it, because the provider only ever
  sees the full libvirt domain name, and hands it to the clone under a private
  key in the VM's info, the way the degradation list travels.
- **Set**: `--hostname <resolved>`, for `/etc/hostname` and the distro files.
- **`/etc/hosts` and cloud-init**: a small generated POSIX `sh` script run with
  `--run`, *before* `--hostname` so it can still read the template's name.
  It runs inside the guest, so it needs no host perl (F13), and it tests for
  every file before touching it (F14). Its only requirement, guest arch matching
  the host's, always holds under KVM. It:
  - treats a loopback line as a self-line when it names the template or is
    `127.0.1.1` (Debian's address for the machine's own name), and replaces
    every non-localhost name on it with the clone's (F16); every other line is
    left byte-for-byte alone, and a self-line is added if none names the clone;
  - never rewrites a `localhost*` name, even for a template called `localhost`;
  - when `/etc/cloud` exists, writes `cloud.cfg.d/99-boxman-hostname.cfg` with
    `preserve_hostname: true` plus `hostname` and `fqdn`, and replaces
    `{{fqdn}}` / `{{hostname}}` in cloud-init's `hosts.*.tmpl` with the literal
    names.
- **Why the hosts template**: a template whose own user-data sets `hostname:`
  with `manage_etc_hosts: true` — every shipped cloud-init box does — has
  cloud-init re-render `/etc/hosts` on *every* boot from its hosts template,
  taking the name from user-data, which outranks any `cloud.cfg.d` drop-in. The
  template file is the one input user-data cannot override, and its own header
  names it as where a persistent change belongs. Rejected alternatives: removing
  `update_etc_hosts` from `cloud.cfg`'s module list (editing a distro conffile,
  and impossible with `--edit` on a guest without cloud-init); a fresh NoCloud
  seed per clone (a new instance-id re-runs every per-instance module of the
  template's user-data on every clone, and does nothing for a sealed template).
- **Validation**: `hostname_problem()` in `src/boxman/utils/hostnames.py` — RFC
  1123 labels, 253 characters in total, a dotted value written as given,
  booleans and numbers refused. It runs at config time in a new
  `validate_clone_identity_config()`, called beside
  `validate_direct_boot_config()` at the top of `provision` and `update`, before
  anything is created. Not `normalize_v2_config()` as first planned: that only
  runs for `version: '2.0'` configs. The same validator now also refuses a bad
  `clone_*` policy value there, instead of first inside a clone worker.
- **An invalid VM key** (`my_vm`) is a fine ssh alias but not a hostname. Under
  `auto` it warns and the guest keeps its template's name; under `required` it
  is a `ConfigError`. So a config that works today does not break. None of the
  45 VMs in the shipped boxes is affected.

### 5.3 #202 — surfacing (PR 3)

The notices already exist and, after §3.4, already name the property. This
issue is about where they surface:

- a closing summary after `up` / `provision` / `update`, one line per VM naming
  the properties kept and the cause, printed at the end of the run rather than
  interleaved;
- `check_prerequisites.py` naming the consequence when `virt-sysprep` is
  missing — clones keep their template's identity under `auto` and fail under
  `required`.

## 6. Config surface

```yaml
vms:
  node01:
    hostname: node01            # now actually reaches the guest
    clone_machine_id: auto      # unchanged
    clone_hostname: auto        # new (#200)
    clone_ssh_host_keys: auto   # new (#201)
```

Defaults keep every clone from now on getting its declared name and fresh host
keys. Already-provisioned VMs are untouched — this runs only at clone time.

## 7. Test plan

**Unit** (host-side, no libvirt) — the plan builder is pure, so most of this is
cheap:

- the invocation carries exactly the operations and customizations the enabled
  policies imply, and nothing when all three are `off`;
- the §3.3 invariant: customizations never appear without `customize`;
- strictest-policy resolution: `required` + `auto` on one failing pass discards;
  all-`auto` records one notice per enabled property and continues;
- `clone_machine_id: off` alongside an enabled property warns rather than raising;
- hostname validation rejects `-bad`, a 64-character label, 254 characters,
  `true` and `42` at config time.

**Integration** (test-runner VM, nested KVM) — the tier that re-checks §2
against real guests:

- two clones of one template have pairwise-distinct host-key fingerprints for
  every type, each different from the template's, and `sshd` accepts a
  connection on first boot (EL **and** Debian/Ubuntu templates, per #201);
- two clones with different `hostname:` each boot with `hostnamectl --static`
  equal to their declared name, `/etc/hosts` has no entry for the template's
  name, and both hold after a guest reboot — run once sealed, once unsealed;
- a VM with no `hostname:` gets its VM key and `boxman ssh <vm>` reaches it;
- `clone_hostname: off` reproduces today's behaviour;
- two clones have **different** machine IDs (the §4 upgrade).

## 8. Docs to update, per `AGENTS.md`

`.claude/skills/boxman/SKILL.md`, `agents/boxman-user.md` (both currently read
as if `hostname:` already names the guest), `agents/boxman-developer.md` for the
pass architecture, `doc/tutorial/README.md`'s clone-policy section including the
§4 caveat removal, and the `data/templates/` config comments.

## 9. Open questions

**Settled during PR 1**, by running the implementation's own invocation
against a disk carrying template host keys:

- *Ownership of uploaded host-key files.* Resolved, and it took two attempts.
  `--upload` preserves the source uid (F10), so the first implementation left
  the guest's private host keys owned by the hypervisor user. The obvious fix,
  `--chown 0:0:<path>`, worked on 1.52.2 and then failed on the test-runner
  VM's 1.52.0, which rejects every form of its own documented syntax (F11).
  Settled with a tar archive carrying uid, gid and mode (F12), verified on
  1.52.0 as `uid 0 / gid 0`, mode `0600` for private keys and `0644` for
  public ones.
- *Whether a degraded `auto` pass is noticeable.* It is not, which is #202's
  whole point: the first integration run showed two clones sharing an rsa host
  key, and the cause — `invalid format for '--chown' parameter` — was only
  visible in boxman's own output, which the test fixture hides. The failure
  was caught by asserting on the guests, not by the provisioning step, which
  reported success.
- *Whether the staged temp directory is reachable under the docker-compose
  runtime.* Confirmed: it is bind-mounted at the same absolute path, which is
  what made the uploads resolve in the container.

**Settled since:**

- `sshd` accepts the installed keys and starts on first boot, on the EL and
  Ubuntu boxes (PR 1's integration run).
- `--mkdir` is `mkdir -p` (F15) — moot now, since the #200 script creates the
  drop-in directory itself.
- The distro hostname files: `--hostname` handles them, and every shipped
  template is a systemd distro where `/etc/hostname` is authoritative.

**Still open:**

- The host-key integration test inspects the *booted* guest. It skips, rather
  than passes, when it has no freshness evidence (no readable template keys and
  no sibling clone), but it still cannot prove the offline `--tar-in` itself: a
  guest whose pass deleted the keys and failed to install new ones, and that
  regenerates missing keys at boot, would also pass. A pre-boot inspection of
  the shut-off clone against the staged fingerprints was proposed in review and
  not built.

## 10. A note on `agents/`

`AGENTS.md` requires `agents/boxman-user.md` and `agents/boxman-developer.md`
to be updated whenever the config surface changes. Those files do not exist on
`main`: they live only on the unmerged branch `doc/agent-definitions`, whose
`boxman-user.md` documents `clone_machine_id` in two places. They therefore
cannot be updated from this branch without creating a conflicting copy, and
need the same treatment when that branch lands.
