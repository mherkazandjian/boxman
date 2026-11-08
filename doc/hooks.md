# `hooks:` -- tasks that `boxman up` runs before and after itself

A project can name some of its `tasks:` as hooks of `boxman up`:

```yaml
tasks:
  check:
    description: "check inputs and capacity, change nothing"
    command: scripts/check.sh
  configure:
    description: "configure what boxman up brought up"
    command: scripts/configure.sh

hooks:
  pre_up: [check]          # before up changes anything
  post_up: [configure]     # after up succeeded
```

With no `hooks:` key, `boxman up` behaves exactly as without this feature:
no hook runs, libvirt is not read for a record, and no file is written.

## The key

- `hooks` is a mapping with at most two keys, `pre_up` and `post_up`. Each
  is a list of task names, run in the order given. An empty list, or `null`,
  is the same as no entry.
- Every name must be a task of the same config whose `command` is a
  non-empty string. Anything else -- another phase, a name that is not a
  task, a task without a string command -- stops `boxman up` with a
  configuration error before it does anything.
- Only `boxman up` runs hooks. `boxman provision`, `update` and the rest do
  not, and `boxman run <task>` still runs any of these tasks on its own.

## When the hooks run

0. boxman takes the project's up lock (`~/.config/boxman/cache/locks/up-<project>.lock`)
   and holds it until the last hook has finished. A second `boxman up` of the
   same project with hooks fails at once, before any hook, changing nothing.
1. The `pre_up` hooks run first, before `boxman up` changes anything: no
   template is built, no network defined, no VM created or started. A
   `pre_up` hook is the place for checks that must pass before anything
   exists.
2. With `post_up` hooks configured, boxman reads the UUIDs of the project's
   libvirt domains and networks (only the names the config declares).
3. `boxman up` itself runs, unchanged: a fresh provision, a start or resume
   of stopped VMs, or the "all VMs already running" path.
4. boxman reads the UUIDs again and writes the [up record](#the-up-record).
5. The `post_up` hooks run, after an `up` that succeeded on any of those
   paths.

A hook that exits non-zero stops `boxman up` there: the hooks after it do
not run, and `boxman up` exits non-zero (2, like any boxman error).

- A failed `pre_up` hook: nothing was changed.
- A failed `post_up` hook: the infrastructure stays as `boxman up` left it;
  the next `boxman up` takes the "already running" path and runs the
  `post_up` hooks again, so `post_up` hooks should be safe to rerun.

A Ctrl-C while a hook runs reaches the hook as well, as it does a
`boxman run` task: the hook is stopped, the hooks after it do not run, and
`boxman up` ends as any interrupted boxman command does (by SIGINT, exit
status 130), with its up lock released.

## What a failed or partial `up` does

- `boxman up` itself fails (a VM that never started, a partial state
  without `--force`, a provider error): no `post_up` hook runs, the error is
  reported as before, and the up record is written with `"status":
  "failed"`, listing what had been created by then. If the workspace does
  not exist yet, nothing was created and no record is written.
- Some project VMs exist and others do not, without `--force`: `boxman up`
  refuses as it always has; the `pre_up` hooks have run, no `post_up` hook
  does.

## `--force`

`--force` changes `boxman up` exactly as it does without hooks, on two
paths, where `up` otherwise refuses:

- some of the project's VMs exist and others do not: `up --force`
  deprovisions the project and provisions it again;
- no VM exists but the project is still registered in boxman's cache (a
  stale registration): `up --force` deprovisions first, then provisions.

A project with no VMs and no registration is provisioned with or without
it, and a complete project is started, resumed or reconciled, never
recreated, with or without it. The hooks run as for any `up` and get
`BOXMAN_UP_FORCE=1`, so a `pre_up` hook can refuse a run it does not
support. Resources recreated by that partial-state reprovision have new
UUIDs and are listed as `created` in the record.

## Environment and working directory

Every hook runs as a `boxman run` task would, with these variables added:

| variable | value |
| --- | --- |
| `BOXMAN_HOOK` | `pre_up` or `post_up` |
| `BOXMAN_UP_RUN_ID` | an id unique to this `boxman up`, also in the record |
| `BOXMAN_UP_FORCE` | `1` with `--force`, else `0` |
| `BOXMAN_UP_RECORD` | `post_up` only: the path of the up record |

Differences from `boxman run`, because a `pre_up` hook runs before the
workspace exists:

- hooks run in the task's own `workdir:` if it sets one, else in the
  directory of conf.yml (not the workspace);
- `pre_up` hooks get the process environment only, not the workspace env
  file; `post_up` hooks get the workspace environment as `boxman run` does.

## The up record

Before the `post_up` hooks run, boxman writes
`<workspace.path>/boxman-up-record.json` (mode 0600, replaced atomically;
`BOXMAN_UP_RECORD` is its absolute path, a relative `workspace.path` being
resolved against the directory boxman was run from):

```json
{
  "boxman_up_record": 1,
  "project": "myproject",
  "run_id": "4f0c...",
  "status": "succeeded",
  "force": false,
  "finished_at": "2026-11-03T13:29:32Z",
  "created": {
    "domains": {"bprj__myproject__bprj_c1_vm1": "<uuid>"},
    "networks": {"bprj__myproject__bprj__clstr__c1__clstr__net1": "<uuid>"}
  },
  "present": {
    "domains": {"bprj__myproject__bprj_c1_vm1": "<uuid>"},
    "networks": {"bprj__myproject__bprj__clstr__c1__clstr__net1": "<uuid>"}
  }
}
```

- `created`: what this `up`'s own operations created or recreated: after a
  provision it ran, every project domain and network present with a UUID
  it did not have before; on the other paths (start, resume, reconcile),
  only the networks that `up`'s network reconciliation defined or
  recreated. Resources that merely appeared while it ran are never listed.
- `present`: all of the project's domains and networks after this `up`.
- `status`: `succeeded`, or `failed` for an `up` that raised (then no
  `post_up` hook runs; the record is for diagnosis).

The record lets a `post_up` hook take ownership of what boxman created
instead of trusting whatever it finds under the project's names: check that
`run_id` equals `BOXMAN_UP_RUN_ID` (a record left by an earlier `up` has
another id), then take the UUIDs from `created`. Libvirt is read with every
query's exit status checked; a failed read stops `boxman up` instead of
producing an incomplete record.

`boxes/talos-unattended` uses both hooks: its preflight runs as `pre_up`,
and its install driver as `post_up`, taking ownership of the VMs and the
network from the record.
