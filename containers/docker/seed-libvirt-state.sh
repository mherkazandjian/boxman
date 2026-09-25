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
# Paths TARGET already holds are kept: never replaced, removed or rewritten,
# so an image upgrade cannot clobber a live installation with defaults. Each
# must still be usable as what the image has there, or it is reported:
#   - where the image has a directory, a directory;
#   - where the image has a regular file, a regular file;
#   - where the image has a symlink, anything but a directory. The image's
#     links point into the container's own tree, so whether one resolves is
#     libvirt's business rather than this script's.
# A symlink in TARGET counts as what it points at, so a dangling one is kept
# but stands in for neither a directory nor a file.
#
# Missing paths are restored one at a time, never by copying a tree. A
# directory is made with mkdir, which refuses to replace anything, and then
# filled entry by entry. Anything else is copied, metadata and all, into a
# staging directory beside its destination and then hard-linked into place.
# The link is atomic and fails if the destination exists, so a copy cut
# short — a full disk, a container stopped mid-copy — is never published,
# and a path that appears while the copy runs is never overwritten. Staging
# directories are named .boxman-seed.XXXXXX; one left by a killed run is
# removed by the next. Hard links need the host directory on a filesystem
# that has them, as every native Linux one does.
#
# The flip side of restoring whatever is missing: a file the image ships
# comes back on the next start if it is deleted from TARGET — including
# after `virsh net-undefine default`, which the entrypoint would redefine
# regardless.
#
# Prints what it restored. Exits non-zero, naming each path, when TARGET is
# still missing something or holds something it cannot use; what that means
# for the container is the caller's decision.

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

# The staging directory in use, removed however the script ends. SIGKILL
# is the exception, and the next run's remove_stale_staging covers it.
staging=
trap 'if [ -n "$staging" ]; then rm -rf -- "$staging"; fi' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

exists() {
    [ -e "$1" ] || [ -L "$1" ]
}

is_real_dir() {
    [ -d "$1" ] && [ ! -L "$1" ]
}

# What the image has at $1, not following symlinks.
image_kind() {
    if [ -L "$1" ]; then echo "a symlink"
    elif [ -d "$1" ]; then echo "a directory"
    elif [ -f "$1" ]; then echo "a regular file"
    else echo "a $(stat -c %F -- "$1")"
    fi
}

# What TARGET has at $1, following symlinks.
target_kind() {
    if [ -L "$1" ] && [ ! -e "$1" ]; then echo "a dangling symlink"
    elif [ -d "$1" ]; then echo "a directory"
    elif [ -f "$1" ]; then echo "a regular file"
    else echo "a $(stat -L -c %F -- "$1")"
    fi
}

# Whether the existing $2 can serve as the image's $1.
compatible() {
    if [ -L "$1" ]; then [ ! -d "$2" ]
    elif [ -d "$1" ]; then [ -d "$2" ]
    elif [ -f "$1" ]; then [ -f "$2" ]
    else [ "$(stat -L -c %F -- "$2" 2>/dev/null)" = "$(stat -c %F -- "$1")" ]
    fi
}

# Make directory $2 with $1's mode and, where permitted, owner — the
# ownership part is best effort, as it is for `cp -a` run by a non-root user.
make_dir_like() {
    err=$(mkdir -m "$(stat -c %a -- "$1")" -- "$2" 2>&1) || return 1
    chown --reference="$1" -- "$2" 2>/dev/null || true
}

# Restore the missing $2 from $1 without ever replacing anything at $2.
# Sets err when it fails.
publish() {
    if is_real_dir "$1"; then
        make_dir_like "$1" "$2"
        return
    fi
    if ! staging=$(mktemp -d -- "${2%/*}/.boxman-seed.XXXXXX" 2>&1); then
        err=$staging
        staging=
        return 1
    fi
    err=$(cp -a -- "$1" "$staging/entry" 2>&1) &&
        err=$(ln -P -T -- "$staging/entry" "$2" 2>&1)
    rc=$?
    rm -rf -- "$staging"
    staging=
    if [ "$rc" -gt 128 ] && [ -z "$err" ]; then
        err="the copy was killed by SIG$(kill -l "$((rc - 128))")"
    elif [ "$rc" -ne 0 ] && [ -z "$err" ]; then
        err="exit status $rc"
    fi
    return "$rc"
}

remove_stale_staging() {
    for stale in "$1"/.boxman-seed.??????; do
        if is_real_dir "$stale" && rm -rf -- "$stale"; then
            echo "Removed $stale, left behind by an interrupted run"
        fi
    done
}

if ! exists "$target"; then
    if ! err=$(mkdir -p -- "$(dirname -- "$target")" 2>&1) ||
            ! make_dir_like "$pristine" "$target"; then
        echo "cannot create $target: $err" >&2
        exit 1
    fi
elif [ ! -d "$target" ]; then
    echo "$target is $(target_kind "$target"), not a directory" >&2
    exit 1
fi
remove_stale_staging "$target"

was_empty=
if [ -z "$(ls -A -- "$target" 2>/dev/null)" ]; then
    was_empty=1
fi

restored=()
problems=()
blocked=
quiet=
while IFS= read -r -d '' src; do
    rel="${src#"$pristine"/}"
    # nothing below a directory that is missing or unusable can be restored,
    # and the directory itself has been reported
    if [ -n "$blocked" ] && [ "${rel#"$blocked"/}" != "$rel" ]; then
        continue
    fi
    dst="$target/$rel"

    if ! exists "$dst"; then
        if publish "$src" "$dst"; then
            # a directory restored whole is listed once, not entry by entry
            if [ -z "$quiet" ] || [ "${rel#"$quiet"/}" = "$rel" ]; then
                restored+=("$rel")
                if is_real_dir "$src"; then
                    quiet="$rel"
                fi
            fi
            continue
        fi
        if ! exists "$dst"; then
            problems+=("$rel: could not be restored: $err")
            if is_real_dir "$src"; then
                blocked="$rel"
            fi
            continue
        fi
        # It appeared while this script was restoring it, so someone else
        # put it there; it is judged like anything else already present.
    fi

    if ! compatible "$src" "$dst"; then
        problems+=("$rel: $(target_kind "$dst") where the image has $(image_kind "$src")")
        if is_real_dir "$src"; then
            blocked="$rel"
        fi
    elif is_real_dir "$src"; then
        remove_stale_staging "$dst"
    fi
done < <(find "$pristine" -mindepth 1 -print0)

if [ "${#restored[@]}" -gt 0 ]; then
    if [ -n "$was_empty" ] && [ "${#problems[@]}" -eq 0 ]; then
        echo "Seeded $target from the image's pristine copy"
    else
        echo "Restored ${#restored[@]} path(s) missing from $target" \
             "from the image's pristine copy:"
        printf '    %s\n' "${restored[@]}"
    fi
fi

if [ "${#problems[@]}" -gt 0 ]; then
    echo "Could not repair ${#problems[@]} path(s) in $target; what is" \
         "there has been left as it is:" >&2
    printf '    %s\n' "${problems[@]}" >&2
    exit 1
fi
