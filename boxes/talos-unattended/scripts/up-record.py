#!/usr/bin/env python3
"""
Read boxman's up record for the install driver's post_up hook.

    up-record.py RECORD RUN_ID PROJECT NETWORK DOMAIN...

RECORD is the file boxman up names in BOXMAN_UP_RECORD (doc/hooks.md),
RUN_ID the BOXMAN_UP_RUN_ID boxman gave this hook, and NETWORK and the
DOMAINs this box's project network and VMs. The record must be this run's
(same run id and project), report a succeeded up, and list every DOMAIN and
NETWORK as present. Then it says which kind of up this was:

    mode created    every DOMAIN and the NETWORK were created by this up
    mode existing   none of them was: they were all there already

followed by "uuid <name> <uuid>" for each of them, as boxman read it. An up
that created some of them but not others is neither: exit status 3. Any
other problem with the record: exit status 1. Messages go to stderr.
"""

from __future__ import annotations

import json
import sys


def fail(msg: str, status: int = 1) -> None:
    print(f"up-record: error: {msg}", file=sys.stderr)
    sys.exit(status)


def main(argv: list[str]) -> int:
    if len(argv) < 6:
        fail("usage: up-record.py RECORD RUN_ID PROJECT NETWORK DOMAIN...")
    path, run_id, project, network, domains = argv[1], argv[2], argv[3], argv[4], argv[5:]
    try:
        with open(path) as fobj:
            record = json.load(fobj)
    except (OSError, ValueError) as exc:
        fail(f"cannot read boxman's up record {path}: {exc}")
    if not isinstance(record, dict) or record.get("boxman_up_record") != 1:
        fail(f"{path} is not a boxman up record (version 1)")
    if record.get("run_id") != run_id:
        fail(f"{path} belongs to another boxman up (run {record.get('run_id')}, not {run_id}); "
             "refusing to take ownership from it")
    if record.get("project") != project:
        fail(f"{path} is for project {record.get('project')}, not {project}")
    if record.get("status") != "succeeded":
        fail(f"{path} reports a boxman up that did not succeed ({record.get('status')})")
    tables = {}
    for part in ("created", "present"):
        value = record.get(part)
        if not (isinstance(value, dict) and isinstance(value.get("domains"), dict)
                and isinstance(value.get("networks"), dict)):
            fail(f"{path}: '{part}' is missing or malformed")
        tables[part] = value
    present, created = tables["present"], tables["created"]
    wanted = [("domains", name) for name in domains] + [("networks", network)]
    missing = [name for kind, name in wanted if not present[kind].get(name)]
    if missing:
        fail(f"boxman up finished, but its record does not list {', '.join(missing)} as present")
    made = [name for kind, name in wanted if name in created[kind]]
    if len(made) == len(wanted):
        mode = "created"
    elif not made:
        mode = "existing"
    else:
        kept = [name for kind, name in wanted if name not in created[kind]]
        fail(f"this boxman up created {', '.join(made)} but found {', '.join(kept)} already there; "
             "a partly recreated lab cannot be taken over", 3)
    for kind, name in wanted:
        if created[kind].get(name, present[kind][name]) != present[kind][name]:
            fail(f"{path}: {name} has different uuids under 'created' and 'present'")
    print(f"mode {mode}")
    for kind, name in wanted:
        print(f"uuid {name} {present[kind][name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
