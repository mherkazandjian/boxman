#!/bin/bash
#
# Restore what a bind-mounted libvirt state directory lacks from the image's
# pristine copy of it.
#
#   seed-libvirt-state.sh TARGET PRISTINE
#
# /etc/libvirt and /var/lib/libvirt/qemu are bind-mounted from the host so
# their state outlives the container (#164 FB-2), and the mount hides
# everything the image configured there. This copies every path PRISTINE
# holds that TARGET does not: all of it into an empty directory on a first
# run, a single file into a damaged one.
#
# The decision is made per path, never on whether TARGET is empty (#205).
# A directory holding only the nwfilter/ and secrets/ that libvirtd creates
# for itself used to pass for seeded, and the default network it lacked
# then wedged the container in a restart loop.
#
# A path TARGET already holds, in any form, is never touched: the host
# directory is the authority for what is in it, so an image upgrade cannot
# clobber a live installation with defaults. The flip side is that a file
# the image ships comes back on the next start if it is deleted from
# TARGET — `virsh net-undefine default` included, which the entrypoint
# would redefine regardless.
#
# Prints what it restored. Exits non-zero, naming each path it could not
# restore, when TARGET is still missing something; what that means for the
# container is the caller's decision.

set -u

if [ "$#" -ne 2 ]; then
    echo "usage: $(basename "$0") TARGET PRISTINE" >&2
    exit 2
fi
target="${1%/}"
pristine="${2%/}"

if [ ! -d "$pristine" ]; then
    echo "there is no pristine copy of $target at $pristine to restore" \
         "it from; the image predates it and has to be rebuilt" >&2
    exit 1
fi

if ! err=$(mkdir -p -- "$target" 2>&1); then
    echo "cannot create $target: $err" >&2
    exit 1
fi

was_empty=
if [ -z "$(ls -A -- "$target" 2>/dev/null)" ]; then
    was_empty=1
fi

restored=()
failed=()
skip=
while IFS= read -r -d '' src; do
    rel="${src#"$pristine"/}"
    # a directory that could not be restored has been reported already,
    # so do not report everything inside it as well
    if [ -n "$skip" ] && [ "${rel#"$skip"/}" != "$rel" ]; then
        continue
    fi
    dst="$target/$rel"
    # -L as well: a dangling symlink is still something TARGET holds, and
    # cp refuses to write through one, which would read as damage
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        continue
    fi
    # find lists a directory before its contents, so a missing directory
    # is copied whole here and its contents are skipped as present
    if err=$(cp -a -- "$src" "$dst" 2>&1); then
        restored+=("$rel")
    else
        failed+=("$rel: $err")
        if [ -d "$src" ] && [ ! -L "$src" ]; then
            skip="$rel"
        fi
    fi
done < <(find "$pristine" -mindepth 1 -print0)

if [ "${#restored[@]}" -gt 0 ]; then
    if [ -n "$was_empty" ]; then
        echo "Seeded $target from the image's pristine copy"
    else
        echo "Restored ${#restored[@]} path(s) missing from $target" \
             "from the image's pristine copy:"
        printf '    %s\n' "${restored[@]}"
    fi
fi

if [ "${#failed[@]}" -gt 0 ]; then
    echo "Could not restore ${#failed[@]} path(s) missing from $target:" >&2
    printf '    %s\n' "${failed[@]}" >&2
    exit 1
fi
