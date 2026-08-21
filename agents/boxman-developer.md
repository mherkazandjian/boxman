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
  hierarchy and exit codes, the sudo-wrapping rules, how config precedence and
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
    cli_parser.py      # all argparse wiring; parse_args() is the only public surface
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
    local.py           # commands run on the host (no-op wrapping)
    docker_compose.py  # commands wrapped with docker exec into a libvirt container
  netlab/              # containerlab integration + host shared bridges
  utils/               # jinja env, references, io, shell, hostnames, parsers
  config_cache.py      # BoxmanCache: which projects/networks are provisioned
  image_cache.py       # base-image / ISO download cache
  exceptions.py        # the error hierarchy
  loggers/             # verbosity levels and formatting
tests/                 # ~78 modules, marker-tiered
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
| `config.py` | loading, merging and rendering config; `boxman conf` |
| `workspace.py` | workspace/cluster file generation, inventory, env.sh, ansible.cfg |
| `naming.py` | `full_network_name()`, adapter `network_source` resolution |
| `ssh.py` | ssh_config generation, host aliases, `boxman ssh` |
| `images.py` | templates, base images, ISO resolution, OCI push/pull, import-image |
| `networks.py` | network reconcile orchestration, isolation self-healing |
| `vms.py` | clone/configure/start, the `update` diff and its application |
| `snapshots.py` | every snapshot verb + `_select_vm_targets()` |
| `flows.py` | `provision`, `up`, `down`, `deprovision`, `destroy` |
| `compose.py` | docker-compose-provider cluster lifecycle |
| `control.py` | suspend / resume / save / start |
| `netlab.py` | containerlab verbs |
| `misc.py` | `ps`, `list`, `run`, `exec`, `pxe-boot`, storage verbs |

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

Subclass `RuntimeBase`, implement `wrap_command()`, register it in the
`create_runtime` map. Anything that shells out must go through the runtime's
wrapping, or it will silently run on the wrong side of the container boundary.

### A config key

- Add it to `data/templates/conf.libvirt.yml` (or `boxman.yml`) with a comment
  — that file is the canonical example users copy.
- Validate it where it is first read, with a message that names the key and
  what to do. Prefer failing before any disk or libvirt I/O: several validators
  (`validate_base_images()`, network validation) deliberately run up front and
  aggregate every problem into one error rather than dying halfway through a
  parallel clone.
- If it affects a live resource, decide whether `update` can apply it hot,
  needs a restart, or is structural — and make the reconcile plan say which.

---

## Conventions

### Errors and exit codes

Use the hierarchy in `exceptions.py` rather than bare `Exception`:

```
BoxmanError
├── ConfigError            # bad conf.yml / boxman.yml, unresolvable ${env:VAR}
├── ProvisionError         # a provisioning step failed
│   ├── CloneSanitizerError
│   │   ├── CloneSanitizerUnavailableError
│   │   └── CloneCleanupError
│   ├── NetworkError
│   └── TemplateError
├── SnapshotError
└── RuntimeUnavailable     # docker daemon down, libvirtd unreachable — often retriable
```

Always chain: `raise ProvisionError(...) from exc`. `main()` turns any
`BoxmanError` into a one-line `log.error` + `sys.exit(2)` with no traceback, so
the message *is* the user interface — make it say what to change. A
`NotImplementedError` from the virtualbox stubs is likewise translated to exit
2 with a "Phase 1" note.

Do not exit 0 on a failure path. Aborts on config/restore/update were
deliberately converted from `exit 0` to raises; do not reintroduce the pattern.

### Sudo

`_should_use_sudo_for_command()` resolves, first match wins:
`force_sudo_commands` → `rm` (never, because unlinking needs write permission
on the boxman-owned parent dir and a prompting sudo makes cleanup fail
silently) → `sudo_skip_commands` → global `use_sudo`. Matching is on the
basename of the first token. If you add a command that may need root, think
about which bucket it belongs in rather than blanket-wrapping it.

### Parallelism

Parallel VM operations go through the shared `_run_parallel` helper, not raw
`multiprocess.Process` calls. Failures in one worker must not strand the
others; propagate an aggregate result the caller can report on.

### Logging

Verbosity is a level, not a boolean: default terse status lines, `-v` info,
`-vv` debug with `[time LEVEL file:func]`, `-vvv` also echoes shell commands.
`-q` is warnings and errors. Both flag positions (before and after the
subcommand) are reconciled by `resolve_verbosity()`, with a `BOXMAN_VERBOSITY`
env fallback. A message that a user must act on is a warning or error; progress
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
make test-vm-test                    # default selection (unit + smoke)
make test-vm-test tier=integration   # the Docker + nested-KVM tier
make test-vm-test pytest_args="-k test_name"
make test-vm-destroy                 # tear it down
```

Host-side targets exist for quick local iteration but are not CI-grade:

```bash
make test                                  # all default-selected tests
make test verbose=1                        # -v
make test pytest="tests/test_runtime.py"   # one file
make test pytest_args="-k test_name"       # one test
make test-integration                      # docker-compose runtime integration tests
make test-provision                        # box provisioning integration tests
make test-dc-e2e                           # docker-compose *provider* e2e tests
```

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
  from config; fix the generator, never the output.
- **`env.sh` and `ansible.cfg` are preserved once they exist** (matched by
  basename). That is intentional, and it means a stale hand-edited `env.sh`
  silently shadows config changes — worth remembering when a bug report says
  "my change had no effect".
- **The projects cache is runtime-scoped and written atomically**, and it
  tolerates a corrupt file rather than crashing. Keep both properties if you
  touch `config_cache.py`.
- **`make loc` / `make loc-detailed`** report lines of code by category if you
  need to size a change.

---

## Review checklist for a change

- Does it hook into `up`, not only `provision`?
- Does it go through the runtime wrapper for anything that shells out?
- Does it use `_select_vm_targets()` for `--vms` / `--cluster`?
- Does it raise a typed `BoxmanError` with an actionable message, chained from
  the original?
- Does it validate up front and aggregate errors, rather than failing halfway
  through a parallel operation?
- Are new config keys in `data/templates/` with a comment?
- Is the user-facing doc updated in the same change, and does it avoid claiming
  more than the code does?
- Is there a test at the right tier, with the right marker?
- Does `ruff check src tests scripts` pass, and the default pytest selection?
