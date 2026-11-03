# Shared by the host-side scripts of boxes/talos-unattended (install.sh,
# preflight.sh, control-exec.sh, status.sh). Sourced, never run. The caller
# sets PROG before sourcing.
#
# Inputs are read the way gen-config.sh reads them: versions.env pins are
# defaults for any variable the environment does not set, then the inputs
# file (--inputs, default inputs.env beside conf.yml when it exists) is
# sourced over them.

BOX_DIR=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
SCRIPTS=$BOX_DIR/scripts
PYTHON=${TALOS_PYTHON:-python3}

log() { printf '%s: %s\n' "${PROG:-talos}" "$*" >&2; }
die() { printf '%s: error: %s\n' "${PROG:-talos}" "$*" >&2; exit "${DIE_STATUS:-1}"; }
# drift: the cluster is not what this driver set up; a person decides what next
drift() { DIE_STATUS=3 die "$@"; }

# load_inputs [FILE]: the version set, then the private inputs file
load_inputs() {
    local file=${1:-} versions=$BOX_DIR/versions.env line key value
    [ -r "$versions" ] || die "cannot read the version set $versions"
    PINNED=()
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        [[ $line =~ ^([A-Z][A-Z0-9_]*)=([A-Za-z0-9._:/@+-]*)$ ]] \
            || die "$versions: expected KEY=value, got: $line"
        key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
        PINNED+=("$key=$value")
        [ -n "${!key+set}" ] || export "$key=$value"
    done < "$versions"
    if [ -z "$file" ] && [ -e "$BOX_DIR/inputs.env" ]; then
        file=$BOX_DIR/inputs.env
    fi
    INPUTS_FILE=$file
    if [ -n "$file" ]; then
        [ -r "$file" ] || die "cannot read inputs file $file"
        set -a
        # shellcheck disable=SC1090
        . "$file"
        set +a
    fi
}

# read_facts: the rendered conf.yml, as box-facts.py prints it
read_facts() {
    local out kind a b c d e f g h
    out=$(cd "$BOX_DIR" && "$PYTHON" "$SCRIPTS/box-facts.py" ${TALOS_CONF:+"$TALOS_CONF"}) \
        || die "cannot render conf.yml (is boxman importable by $PYTHON, and are the inputs complete?)"
    VM_DOMAIN=(); VM_CLUSTER=(); VM_NAME=(); VM_VCPUS=(); VM_MEM=(); VM_DISK=(); VM_MAC=(); VM_ROLE=()
    while read -r kind a b c d e f g h; do
        case "$kind" in
            project)   PROJECT=$a ;;
            uri)       LIBVIRT_URI=$a ;;
            use_sudo)  USE_SUDO=$a ;;
            workspace) WORKSPACE=$a ;;
            admin)     CONTROL_USER=$a ;;
            network)   NETWORK=$a NET_ADDR=$b NET_MASK=$c ;;
            template)  TEMPLATE=$a TEMPLATE_DIR=$b TEMPLATE_DISK=$c ;;
            vm)
                VM_DOMAIN+=("$a"); VM_CLUSTER+=("$b"); VM_NAME+=("$c"); VM_VCPUS+=("$d")
                VM_MEM+=("$e"); VM_DISK+=("$f"); VM_MAC+=("$g"); VM_ROLE+=("$h") ;;
            *) die "box-facts.py: unexpected line: $kind $a" ;;
        esac
    done <<< "$out"
    CONTROL_DOMAIN=""
    local i
    for i in "${!VM_DOMAIN[@]}"; do
        [ "${VM_ROLE[$i]}" = control ] && CONTROL_DOMAIN=${VM_DOMAIN[$i]}
    done
    DRIVER_DIR=${TALOS_DRIVER_DIR:-$WORKSPACE/driver}
    RECORD=$DRIVER_DIR/run.env
    KNOWN_HOSTS=$DRIVER_DIR/known_hosts
    HOST_KEY_ALIAS=control.$PROJECT
    # the control VM's copy of this box, beside its private state
    REMOTE_BOX=${TALOS_CONTROL_BOX:-talos-unattended}
}

# vsh ARGS: virsh on this box's libvirt connection. Read-only queries and the
# guest agent only; the driver never changes a domain, network or volume.
vsh() {
    if [ "${USE_SUDO:-false}" = true ]; then
        sudo -n virsh -c "$LIBVIRT_URI" "$@"
    else
        virsh -c "$LIBVIRT_URI" "$@"
    fi
}

# private_dir DIR: create DIR 0700 (and refuse one others can read)
private_dir() {
    local dir=$1
    ( umask 077 && mkdir -p -- "$dir" ) || die "cannot create $dir"
    chmod 0700 "$dir" || die "cannot make $dir private"
}

# outside_checkouts PATH WHAT: refuse a PATH in, or possibly in, a git
# checkout. Fail closed, like gen-config.sh: the nearest existing directory is
# resolved first, every parent is searched for a .git, and git, when there,
# must positively say "not a git repository".
outside_checkouts() {
    local path=$1 what=$2 probe physical dir answer
    probe=$path
    while [ ! -e "$probe" ]; do probe=$(dirname -- "$probe"); done
    [ -d "$probe" ] || die "$what $path: $probe is not a directory"
    physical=$(cd -P -- "$probe" 2>/dev/null && pwd -P) \
        || die "cannot resolve $probe; refusing to keep $what below it"
    dir=$physical
    while :; do
        [ -x "$dir" ] || die "cannot inspect $dir for a git checkout; refusing to keep $what below it"
        # git's own test, as in gen-config.sh: a .git file or link (worktree,
        # submodule), or a .git directory with a HEAD; one we cannot read
        # counts as a checkout
        if [ -L "$dir/.git" ] || { [ -e "$dir/.git" ] && [ ! -d "$dir/.git" ]; }; then
            die "$what $path is inside a git checkout ($dir/.git); keep cluster secrets out of repositories"
        elif [ -d "$dir/.git" ]; then
            [ -r "$dir/.git" ] && [ -x "$dir/.git" ] || die "cannot inspect $dir/.git; refusing to keep $what below it"
            [ ! -e "$dir/.git/HEAD" ] || die "$what $path is inside a git checkout ($dir/.git); keep cluster secrets out of repositories"
        fi
        [ "$dir" = / ] && break
        dir=$(dirname -- "$dir")
    done
    if command -v git >/dev/null 2>&1; then
        if answer=$(LC_ALL=C git -C "$physical" rev-parse --is-inside-work-tree 2>&1); then
            die "$what $path is inside a git repository; keep cluster secrets out of repositories"
        fi
        case "$answer" in
            *"not a git repository"*) ;;
            *) die "cannot tell whether $path is in a git repository (git: ${answer:-no output})" ;;
        esac
    fi
}

# below PATH DIR: PATH is DIR or lies below it (both absolute, lexically)
below() {
    local path=${1%/} dir=${2%/}
    [ "$path" = "$dir" ] || [ "${path#"$dir"/}" != "$path" ]
}

# record_get KEY: a value from the run record, empty when absent
record_get() {
    [ -r "$RECORD" ] || return 0
    awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$RECORD"
}

# record_set KEY VALUE: replace KEY in the run record, atomically
record_set() {
    local tmp
    tmp=$(mktemp "$RECORD.XXXXXX") || die "cannot write $RECORD"
    { [ -r "$RECORD" ] && awk -F= -v k="$1" '$1 != k' "$RECORD"; printf '%s=%s\n' "$1" "$2"; } > "$tmp"
    chmod 0600 "$tmp" && mv -f "$tmp" "$RECORD" || die "cannot write $RECORD"
}

# domain_state DOMAIN: running, shut off, ... or "absent"
domain_state() {
    local state
    if state=$(vsh domstate "$1" 2>/dev/null); then
        printf '%s\n' "$state" | sed -n '1{s/[[:space:]]*$//;p}'
    else
        echo absent
    fi
}

domain_uuid() { vsh domuuid "$1" 2>/dev/null | sed -n '1{s/[[:space:]]*$//;p}'; }

# lease_ips MAC: the IPv4 leases the fabric network holds for MAC
lease_ips() {
    vsh net-dhcp-leases "$NETWORK" --mac "$1" 2>/dev/null \
        | awk -v mac="$1" 'tolower($3) == tolower(mac) && $4 == "ipv4" { sub(/\/.*/, "", $5); print $5 }' \
        | sort -u
}

# ssh_opts: every ssh to the control VM: the operator key only, the pinned
# host key only, nothing from the user's ssh config, never a prompt
ssh_opts() {
    SSH_OPTS=(-F /dev/null
        -o BatchMode=yes -o IdentitiesOnly=yes -o "IdentityFile=$TALOS_OPERATOR_KEY"
        -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS"
        -o GlobalKnownHostsFile=/dev/null -o "HostKeyAlias=$HOST_KEY_ALIAS"
        -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4
        -o LogLevel=ERROR)
}
