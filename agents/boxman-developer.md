---
name: boxman-developer
description: >-
  Use when working ON the boxman codebase rather than with it — adding or
  changing a CLI subcommand, a provider, a runtime, a config key, a libvirt
  operation, or a docker-compose-provider capability; writing or fixing tests;
  chasing a bug through the manager mixins; reviewing a diff; or preparing a
  change for CI. Triggers include edits under src/boxman/, tests/,
  scripts/installer/, containers/docker/ or doc/; questions about the
  runtime/provider abstractions, the BoxmanManager mixin layout, the exception
  hierarchy and exit codes, the sudo-wrapping rules, the clone identity pass,
  the teardown and disk-creation safety rules, how config precedence and
  merging work, which pytest marker a test belongs to, why a default pytest
  run skips a test, or how to run the integration tier safely. For *using*
  boxman to provision infrastructure, use the boxman-user agent instead.
tools: Bash, Read, Edit, Write, Glob, Grep
---

# boxman — contributor agent

You are working on the boxman source. boxman is a declarative provisioning
manager: a `conf.yml` describes clusters of libvirt VMs (and, from schema v2.0,
docker-compose containers), and the CLI reconciles reality against it. Python
3.10+, packaged with Poetry.

Read the code before changing it. This file tells you where things are and what
the house rules are; it is not a substitute for the module you are about to
edit.

---

## Repository layout

```
src/boxman/
  scripts/
    app.py             # CLI entry point: dispatch, exit codes, per-verb setup
    cli_parser.py      # all argparse wiring: parse_args(), resolve_verbosity()
  manager.py           # BoxmanManager: composes the mixins, owns sessions + runtime
  manager_parts/       # the manager, split by concern (see below)
  abstract/providers.py# ProviderSession protocol
  providers/
    __init__.py        # PROVIDERS registry + create_session() + config merge
    libvirt/           # the primary provider
    docker_compose/    # container clusters (schema v2.0)
    virtualbox/        # registered but Phase 1: config surface only, stubs raise
    session_base.py    # SessionConfigMixin shared by provider sessions
  runtime/
    __init__.py        # create_runtime() factory
    base.py            # RuntimeBase, the abstract runtime
    local.py           # commands run on the host (no-op wrapping)
    docker_compose.py  # commands wrapped with docker exec into a libvirt container
  netlab/              # containerlab integration + host shared bridges
  utils/               # jinja env, references, io, shell, hostnames, HTTP download,
                       # descriptor-based tree removal (retained_tree.py)
  config_cache.py      # BoxmanCache: which projects/networks are provisioned
  image_cache.py       # base-image / ISO download cache
  exceptions.py        # the error hierarchy
  loggers/             # verbosity levels and formatting
tests/                 # ~110 modules, marker-tiered
doc/                   # architecture + user reference docs
boxes/                 # runnable example configs, also used by integration tests
data/templates/        # canonical conf.yml / boxman.yml templates shipped to users
scripts/installer/     # stdlib-only host prerequisites doctor
containers/docker/     # the libvirt container image for the docker runtime
```

### The two abstractions that shape everything

- **Runtime — *where* a provider command executes.** `LocalRuntime` runs it on
  the host; `DockerComposeRuntime` wraps it with `docker exec` into a libvirt
  container. Chosen by `--runtime` or `runtime:` in the app config, and built
  by `create_runtime(name, **kwargs)`. Valid names: `local`, `docker`,
  `docker-compose` (alias for `docker`).
- **Provider — *what* a cluster is made of.** `libvirt` (VMs), `docker-compose`
  (containers), `virtualbox` (registered but non-functional). Built by
  `create_session(provider_type, config)` from the `PROVIDERS` registry. Under
  schema v2.0 a cluster may declare its own `provider:`; `primary_provider_type()`
  returns the project-wide default, which is `libvirt` when unset.

These are **independent axes** that unfortunately share the name
`docker-compose`. The docker-compose *provider* requires the `local` *runtime*
and says so with an explicit error. Keep the two apart in code, in messages,
and in docs.

### `BoxmanManager` and its mixins

`manager.py` keeps only session/runtime ownership and construction; the verbs
live in `manager_parts/`:

| Module | Owns |
|---|---|
| `config.py` | loading and rendering config, schema `version:` handling (v2.0 normalisation) |
| `workspace.py` | workspace/cluster file generation, inventory, env.sh, ansible.cfg |
| `naming.py` | `full_network_name()`, adapter `network_source` resolution |
| `ssh.py` | ssh_config generation, host aliases, admin keys, connection info |
| `images.py` | templates, base images, ISO resolution, OCI push/pull, import-image, `pxe-boot` |
| `networks.py` | network reconcile orchestration, isolation self-healing |
| `vms.py` | clone/configure/start, the `update` diff and its application, clone-identity validation and the closing degradation summary |
| `snapshots.py` | every snapshot verb, the `storage` verbs, `_select_vm_targets()` |
| `flows.py` | `provision`, `up`, `down`, `deprovision`, `destroy`, `destroy-runtime` |
| `compose.py` | docker-compose-provider cluster lifecycle |
| `control.py` | suspend / resume / save / start |
| `netlab.py` | containerlab verbs |
| `misc.py` | `ps`, `list`, `run`, `exec`, `conf`, `ssh` |

When adding a verb, put it in the mixin that owns the concern, not in
`manager.py`. If a helper is needed by two mixins, it belongs in `utils/` or on
the shared base — not duplicated.

⚠ **Do not cite line numbers in comments or docs.** These modules move. Refer
to `manager_parts/vms.py` and the function name instead.

---

## Adding things

### A CLI subcommand

1. Add the subparser in `cli_parser.py`. Reuse the shared parents (`common`,
   `vms_parent`, `cluster_parent`, `cluster_env_parent`, `force_parent`,
   `rebuild_templates_parent`, `recreate_networks_parent`) rather than
   re-declaring identical arguments — drift between copies has caused real bugs.
2. `set_defaults(handler='<name>')`. Dispatch is **name-based**, resolved
   against an allowlist in `app.py`; a handler name not in that allowlist is a
   parser error, so add it there too.
3. Implement `<name>(self, cli_args)` on the right mixin.
4. If the verb touches VMs, select them with `_select_vm_targets(cli_args)` so
   `--vms` / `--cluster` behave consistently. Do not re-implement the filter.
5. If the verb touches provider state, call `_update_sessions_with_runtime()`
   first so the runtime settings are injected.
6. **Hook new subsystems into `up`'s existing-VM branch**, not just into
   `provision`. `up` is the primary entry point, and anything that only runs on
   a fresh provision silently stops reconciling for everyone with an existing
   cluster.

### A provider

Implement the `ProviderSession` protocol (`abstract/providers.py`), reuse
`SessionConfigMixin` for the config surface, and register a lazy factory in
`PROVIDERS`. Factories import their session class **inside** the function so
importing `boxman.providers` never drags in another provider's dependencies.
The `--provider` choices on `import-image` derive from the registry, so they
stay in sync automatically.

### A runtime

Subclass `RuntimeBase`, implement `wrap_command(command, pass_env=None)` and
the `name` property, register it in the `create_runtime` map. Provider
commands (`virsh`, `qemu-img`, …) must go through the runtime's wrapping, or
they will silently run on the wrong side of the container boundary. The
commands that manage the runtime itself (`docker inspect`, `docker cp`,
`docker compose …` on the libvirt container) run on the host on purpose and
are not wrapped.

Either way, a subprocess goes through `boxman.utils.shell.run`, never
`invoke.run` directly: the wrapper defaults `in_stream=False` so nothing reads
the test runner's or CI's stdin, and `tests/test_utils_shell.py` fails on any
`invoke.run(` outside `utils/shell.py`.

`pass_env` names environment variables the wrapped command needs: the local
runtime inherits them, the docker runtime forwards each *name* with `-e`. That
is how a secret reaches a command (`sshpass -e` reads `SSHPASS`) — never put
one on an argv, where `ps` and `/proc` show it.

### A config key

- Add it to `data/templates/conf.libvirt.yml` (or `boxman.yml`) with a comment
  — that file is the canonical example users copy.
- Validate it where it is first read, with a message that names the key and
  what to do. Prefer failing before any disk or libvirt I/O: several validators
  (`validate_base_images()`, `validate_direct_boot_config()`,
  `validate_clone_identity_config()`, network validation) deliberately run up
  front — in `update` as well as `provision` — and aggregate every problem into
  one error rather than dying halfway through a parallel clone.
- If it affects a live resource, decide whether `update` can apply it hot,
  needs a restart, or is structural — and make the reconcile plan say which.

### A clone identity property

Every libvirt clone gets one offline `virt-sysprep` pass, run on the shut-off
clone right after `virt-clone`, that gives it its own machine ID, SSH host keys
and hostname. It lives in `providers/libvirt/clone_vm.py`. Each property has a
per-VM `clone_*` policy key (`auto | required | off`, default `auto`), and
`CloneVM.build_identity_plan()` folds the enabled ones into one
`IdentityPlan`. To add a property:

1. Resolve its key in `CloneVM.__init__` with `_resolve_policy()`, and add it
   to the keys `validate_clone_identity_config()` (`manager_parts/vms.py`)
   checks before anything is created.
2. In `build_identity_plan()`, append an `IdentityProperty` plus the
   `--operations` and/or customizations it contributes (set `needs_customize`
   for the latter). Host-side inputs, such as files to upload, are produced in
   `stage_identity_plan()`.
3. Document the key in `data/templates/conf.libvirt.yml`.

Invariants to keep:

- **One pass**, not one per property — each run boots a libguestfs appliance.
- **Customizations need the `customize` operation.** Without it virt-sysprep
  exits 0 having done nothing; `assert_customize_invariant()` raises instead.
  `customize` always writes a fresh machine ID, so any customizing property
  overrides `clone_machine_id: off` (with a warning).
- **A failure resolves against the strictest enabled policy.** One `required`
  property discards the clone (`discard_unsafe_clone()`) and raises; if all are
  `auto`, a `CloneDegradation` naming every property is recorded and cloning
  continues. The pass is not atomic, so messages say the clone *may* have kept
  its template's identity.
- `provision` and `update` repeat every degradation once at the end, via
  `report_clone_degradations()` in a `finally`, so a later failure cannot hide
  it.

The pass runs only at clone time; an existing VM is never re-identified.

---

## Conventions

### Errors and exit codes

Use the hierarchy in `exceptions.py` rather than bare `Exception`:

```
BoxmanError
├── ConfigError            # bad conf.yml / boxman.yml, unresolvable ${env:VAR}
├── ProvisionError         # a provisioning step failed
│   ├── CloneSanitizerError    # the clone identity pass failed
│   │   ├── CloneSanitizerUnavailableError
│   │   └── CloneCleanupError
│   ├── ImageImportError
│   ├── DiskPathOccupiedError  # a new disk's path already has an entry; nothing written
│   ├── SSHAccessError         # raised last; .failures holds one line per problem
│   ├── NetworkError
│   └── TemplateError
├── SnapshotError
│   └── SnapshotRecoveryError  # revert done, overlays not restored — never retry
└── RuntimeUnavailable     # docker daemon down, libvirtd unreachable — often retriable
```

Always chain: `raise ProvisionError(...) from exc`. `main()` turns any
`BoxmanError` into a one-line `log.error` + `sys.exit(2)` with no traceback, so
the message *is* the user interface — make it say what to change. A
`NotImplementedError` from the virtualbox stubs is likewise translated to exit
2 with a "Phase 1" note.

Do not exit 0 on a failure path. Aborts on config/restore/update were
deliberately converted from `exit 0` to raises; do not reintroduce the pattern.
Dispatch calls `getattr(manager, handler)(args)` and discards the return value,
so a verb that reports failure with `return False` exits 0 — raise instead.
Provider session methods return a bool for success; check every one you call
rather than letting it drop.

### Sudo

The rules live on the libvirt command base (`providers/libvirt/commands.py`).
`_should_use_sudo_for_command()` resolves, first match wins:
`force_sudo_commands` → `rm` (never, because unlinking needs write permission
on the boxman-owned parent dir and a prompting sudo makes cleanup fail
silently) → `sudo_skip_commands` → global `use_sudo`. Matching is on the
basename of the first token. If you add a command that may need root, think
about which bucket it belongs in rather than blanket-wrapping it.

- `use_sudo` describes whether **virsh** needs sudo (and is the fallback for
  ordinary commands); it says nothing about whether a command needs root. A
  command that needs root wherever it runs (`iptables`, `ip link set`, sysctl
  writes) goes through `execute_shell(..., privileged=True)`, which ignores
  `use_sudo` and decides from the execution context: no prefix when already
  root — always the case under the docker runtime, which execs as root —
  `sudo` otherwise, with the force/skip lists still applied first.
- A shell string that chains commands needs a prefix per command from
  `sudo_prefix()`: `execute_shell` only looks at the first word, and a
  hand-written `sudo ` bypasses the force/skip lists entirely.

### Parallelism

The manager's parallel VM operations go through the shared `_run_parallel`
helper (`manager.py`), not raw `multiprocessing.Process` calls. It returns
`(results, failures)` keyed by task label and logs each failure; one worker's
failure must not strand the others. Carry failures to the end of the verb:
finish reconciling what did succeed, then raise one error naming what failed
(`up` is the reference).

### Destructive operations

Anything irreversible acts only on positive evidence; an observation that
fails must read as "stop", never as "nothing there".

- Gate removal on a check that fails closed: `confirm_vm_absent()` (a
  successful `virsh list` without the name), not `is_vm_defined()`, which says
  "absent" when libvirt is unreachable. For files, only `FileNotFoundError`
  from `lstat` means absent — `os.path.exists()` / `isdir()` answer False for a
  path they cannot look up.
- A VM teardown removes files only through `remove_vm_storage()`
  (`providers/libvirt/disk_cleanup.py`), which admits them from an inventory
  taken before undefining and keeps anything another domain uses. libvirt is
  never asked to delete a VM's storage.
- Extra disks boxman attaches are recorded in the domain's `<metadata>`
  (`disk_ownership.py`); detach decisions read that record, never "attached
  but not declared".
- Create disk images with `create_image_exclusive()` (`disk.py`), never a bare
  `qemu-img create`, which truncates whatever holds the path or writes through
  a symlink; an occupied path raises `DiskPathOccupiedError`.
- A recursive delete of a configured path is vetted by `_safe_delete_target()`
  (`manager_parts/flows.py`).

### Logging

Verbosity is a level, not a boolean: default terse `STATUS` lines
(`log.status()`), `-v` info, `-vv` debug with `[time LEVEL file:func]`, `-vvv`
also echoes shell commands. `-q` is warnings and errors. `-v` is accepted
before or after the subcommand and the two are reconciled by
`resolve_verbosity()`, with a `BOXMAN_VERBOSITY` env fallback; `-q` only after
it. A message that a user must act on is a warning or error; progress
narration is info or lower.

### Docs

User-visible behaviour changes need the doc updated in the same change:
`README.md` for CLI and install, `doc/network.md` for networking,
`doc/storage.md`, `doc/image-management.md`,
`doc/docker-compose-provider/` for container clusters. Do not describe a
capability more optimistically than the code delivers — several past fixes were
purely "stop overclaiming" doc corrections.

---

## Build and install

```bash
make build            # poetry build (+ wheel repackage)
make install          # build + pip install --force-reinstall dist/*.whl
make cleaninstall     # clean + install
make full-reinstall   # clean + uninstall + poetry lock + install
make clean            # remove build artifacts and __pycache__
make help             # every target with its description
```

Development mode, no install needed:

```bash
export PYTHONPATH="$PWD/src:$PYTHONPATH"
python3 src/boxman/scripts/app.py <subcommand> <args>
```

---

## Tests

Markers are declared in `pyproject.toml`:

| Marker | Meaning |
|---|---|
| `unit` | fast, no external systems |
| `smoke` | CLI-level (argparse, `--help`, config dry-run) |
| `regression` | guards for fixes already landed |
| `slow` | >5s; excluded from a default run |
| `integration` | needs Docker with compose v2 and `/dev/kvm`; excluded from a default run |

`addopts = "-m 'not slow and not integration'"`, so a bare `pytest` runs the
fast tiers only. Shared fixtures are in `tests/conftest.py`.

### Running them safely

The integration tier creates docker networks, libvirt domains and image
downloads. Run every tier inside the **disposable test-runner VM** rather than
on your workstation:

```bash
make test-vm-up                      # provision the VM (one-time; slow, downloads a template)
make test-vm-sync                    # rsync the repo in and (re)install the venv
make test-vm-test                    # default selection (all but slow/integration)
make test-vm-test tier=integration   # the Docker + nested-KVM tier
make test-vm-test pytest_args="-k test_name"
make test-vm-destroy                 # tear it down
```

`data/dev/test-runner/README.md` lists what the VM carries (docker,
containerlab for the hybrid lab boxes) and the nested-KVM requirement.

Host-side targets exist for quick local iteration but are not CI-grade:

```bash
make test                                  # all default-selected tests
make test verbose=1                        # -v
make test pytest="tests/test_runtime.py"   # one file
make test pytest_args="-k test_name"       # one test
make test-integration                      # docker-compose runtime integration tests
make test-provision                        # box provisioning integration tests
make test-dc-e2e                           # docker-compose *provider* e2e tests
make check-box-images                      # probe every box's image/ISO URLs
```

`check-box-images` needs network but downloads nothing and touches no libvirt,
so it is safe on the host; run it when a box's `image.uri` or `isos:` changes.

`make test-vm-up`, `make test-vm-test tier=integration` and the
`test-provision` target are all **long operations** — minutes, with downloads.
Never fire one off silently; ask, or run it in the background and say so.

### Writing tests

- Give every test a marker. An unmarked test still runs by default, but the
  tiering only works if the marker is right.
- Mock the shell boundary, not the logic. The libvirt provider is tested by
  asserting on the composed `virsh` / `virt-install` command strings and on
  parsed output fixtures; go through the same helpers rather than inventing a
  new mocking style.
- `tests/conftest.py` has the shared pieces: `make_bare_manager()` builds a
  `BoxmanManager` without loading config files, for unit-testing mixin
  methods, and an autouse fixture points boxman's per-user cache dir at a
  per-test temp dir. No test may reach the real `~/.config/boxman/cache`;
  patch `boxman.config_cache.DEFAULT_CACHE_DIR` if one needs a specific dir.
- Regression tests for a landed fix get the `regression` marker and a comment
  naming what broke.
- Integration tests must clean up after themselves; a test that leaves a
  libvirt domain or docker network behind will poison later runs.

---

## CI

`.github/workflows/ci.yml` runs on push and PR against `main`/`polish`, on
Python 3.10 and 3.12:

```
ruff check src tests scripts
python -m pytest tests
```

So the gate is **lint clean plus the default (fast) test selection**. Docker
and KVM steps are deliberately absent — the integration tier is excluded by
design and must not be wired into CI.

Ruff config: `target-version = py310`, `line-length = 100`, rules
`E,F,W,I,N,UP,B` with `E501` ignored. Tests additionally waive `N802/N803/N806`
and `E501`. mypy is configured lenient by default with strict overrides for the
newer clean modules (`boxman.exceptions`, `boxman.utils.decorators`) — extend
that list rather than loosening it when you add a clean module.

Run the gate locally before pushing:

```bash
ruff check src tests scripts
python -m pytest tests
```

---

## Working notes

- **`data/dev/` is development scaffolding**, not a reference for new example
  configs. `boxes/` holds the user-facing examples; `data/templates/` holds the
  canonical schema templates.
- **`.boxman/` directories are generated runtime state.** `make dev-clean`
  removes them (with a prompt); `make boxes-clean` handles root-owned leftovers
  under `boxes/`.
- **Generated artifacts are not source.** `conf.rendered.yml`, a cluster's
  `docker-compose.yml`, `ssh_config` and `inventory/01-hosts.yml` are rewritten
  from config; fix the generator, never the output. `conf.rendered.yml` holds
  every `env()` value resolved, so it is created 0600 — keep it that way.
- **`env.sh` and `ansible.cfg` are preserved once they exist** (matched by
  basename). That is intentional, and it means a stale hand-edited `env.sh`
  silently shadows config changes — worth remembering when a bug report says
  "my change had no effect".
- **The projects cache is runtime-scoped and written atomically**, and it
  tolerates a corrupt file rather than crashing. Keep both properties if you
  touch `config_cache.py`.
- **`make loc` (runs cloc in docker) / `make loc-detailed` (plain Python)**
  report lines of code by category if you need to size a change.

---

## Review checklist for a change

- Does it hook into `up`, not only `provision`?
- Does it go through the runtime wrapper for anything that shells out, with
  any secret passed via `pass_env` rather than on the argv?
- Does a command that needs root use `privileged=True`, not `use_sudo`?
- Does it use `_select_vm_targets()` for `--vms` / `--cluster`?
- Does it raise a typed `BoxmanError` with an actionable message, chained from
  the original — never `return False` from a verb or drop a provider bool?
- Does every delete or overwrite go through the fail-closed gates under
  "Destructive operations"?
- Does it validate up front and aggregate errors, rather than failing halfway
  through a parallel operation?
- Are new config keys in `data/templates/` with a comment?
- Is the user-facing doc updated in the same change, and does it avoid claiming
  more than the code does?
- Is there a test at the right tier, with the right marker?
- Does `ruff check src tests scripts` pass, and the default pytest selection?
