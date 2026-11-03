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
- Every name must be a task of the same config with a `command`. Anything
  else -- another phase, a name that is not a task, a task without a
  command -- stops `boxman up` with a configuration error before it does
  anything.
- Only `boxman up` runs hooks. `boxman provision`, `update` and the rest do
  not, and `boxman run <task>` still runs any of these tasks on its own.

## When the hooks run

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

`boxman up --force` keeps its meaning (deprovision what exists, provision
again). The hooks run as for any `up`, and get `BOXMAN_UP_FORCE=1`, so a
`pre_up` hook can refuse a forced recreation it cannot support. Resources
recreated under the same name have new UUIDs and are listed as `created`
in the record.

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
`<workspace.path>/boxman-up-record.json` (mode 0600, replaced atomically):

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

- `created`: the project's domains and networks that exist after this `up`
  with a UUID they did not have before it, i.e. what this `up` created or
  recreated. Empty on a rerun that found everything in place.
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
