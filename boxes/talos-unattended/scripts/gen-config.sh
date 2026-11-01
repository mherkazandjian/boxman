#!/usr/bin/env bash
# Generate this cluster's Talos secrets and per-node machine configurations.
#
# Runs on the control VM (the install driver ships it there), or anywhere a
# pinned talosctl is on PATH:
#
#   gen-config.sh [--dry-run] [--inputs FILE] --nodes FILE
#
#   --nodes FILE    the node table, one "<name> <role> <ipv4> <mac>" per line,
#                   as scripts/nodes-from-conf.py prints it from conf.yml
#   --inputs FILE   sourced first; holds the TALOS_* variables below
#   --dry-run       validate the inputs and print what would run; touches no
#                   state and runs no talosctl
#
# Required: TALOS_STATE_DIR TALOS_CLUSTER_NAME TALOS_VERSION KUBERNETES_VERSION
#           TALOS_INSTALLER_IMAGE TALOS_INSTALL_DISK TALOS_VIP
#           TALOS_NTP_SERVERS TALOS_POD_CIDR TALOS_SERVICE_CIDR
# Optional: TALOS_PATCH_DIR -- all.yaml, <role>.yaml and <node>.yaml in it are
#           applied in that order, then the generated per-node patch. After
#           the merge, check-node-config.py (beside this script; needs python3
#           with PyYAML) rejects a config whose cluster identity, trust,
#           install inputs, CIDRs, role, hostname, interface or VIP differ
#           from what was generated, or that gained a config document.
#
# State (TALOS_STATE_DIR, 0700; refused when it is, or might be, inside a git
# checkout -- a .git in any ancestor, or git not positively saying "no"):
#   secrets.yaml + secrets.sha256   cluster identity: created once, only in an
#                                   empty state directory, reused on every
#                                   rerun, never regenerated
#   cluster.env                     the identity inputs the secrets belong to
#   talosconfig                     admin client config, created once
#   patches/<node>.yaml             generated per-node patches
#   nodes/<node>.yaml               final machine configs (0600)
#
# A rerun with unchanged inputs changes nothing. Nothing secret is printed:
# only commands and file paths are logged.
set -euo pipefail
umask 077

DRY_RUN=0
NODES_FILE=""
INPUTS=""

log() { printf 'gen-config: %s\n' "$*" >&2; }
die() { printf 'gen-config: error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --nodes)   NODES_FILE=${2:?--nodes needs a file}; shift ;;
        --inputs)  INPUTS=${2:?--inputs needs a file}; shift ;;
        -h|--help) usage ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

if [ -n "$INPUTS" ]; then
    [ -r "$INPUTS" ] || die "cannot read inputs file $INPUTS"
    set -a
    # shellcheck disable=SC1090
    . "$INPUTS"
    set +a
fi

# ---- validate every input before any side effect ---------------------------
REQUIRED="TALOS_STATE_DIR TALOS_CLUSTER_NAME TALOS_VERSION KUBERNETES_VERSION
TALOS_INSTALLER_IMAGE TALOS_INSTALL_DISK TALOS_VIP TALOS_NTP_SERVERS
TALOS_POD_CIDR TALOS_SERVICE_CIDR"
for var in $REQUIRED; do
    val=${!var:-}
    [ -n "$val" ] || die "$var is not set"
    case "$val" in *PLACEHOLDER*|*placeholder*) die "$var is still a placeholder" ;; esac
done

IPV4='^((25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])$'
CIDR4='^((25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])/([0-9]|[12][0-9]|3[0-2])$'

[[ $TALOS_CLUSTER_NAME =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] \
    || die "TALOS_CLUSTER_NAME must be a DNS label"
[[ $TALOS_VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "TALOS_VERSION must be an exact release such as v1.2.3"
[[ $KUBERNETES_VERSION =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "KUBERNETES_VERSION must be an exact release such as 1.2.3"
KUBERNETES_VERSION=${KUBERNETES_VERSION#v}
case "$TALOS_INSTALLER_IMAGE" in
    *:"$TALOS_VERSION"|*:"$TALOS_VERSION"@sha256:*) ;;
    *) die "TALOS_INSTALLER_IMAGE must be tagged $TALOS_VERSION (optionally @sha256:...)" ;;
esac
[[ $TALOS_INSTALL_DISK =~ ^/dev/[a-z0-9/]+$ ]] || die "TALOS_INSTALL_DISK must be a /dev path"
[[ $TALOS_VIP =~ $IPV4 ]] || die "TALOS_VIP must be an IPv4 address"
[[ $TALOS_POD_CIDR =~ $CIDR4 ]] || die "TALOS_POD_CIDR must be an IPv4 CIDR"
[[ $TALOS_SERVICE_CIDR =~ $CIDR4 ]] || die "TALOS_SERVICE_CIDR must be an IPv4 CIDR"
[ "$TALOS_POD_CIDR" != "$TALOS_SERVICE_CIDR" ] || die "pod and service CIDRs must differ"
case "$TALOS_STATE_DIR" in /*) ;; *) die "TALOS_STATE_DIR must be an absolute path" ;; esac
if [ -n "${TALOS_PATCH_DIR:-}" ]; then
    [ -d "$TALOS_PATCH_DIR" ] || die "TALOS_PATCH_DIR $TALOS_PATCH_DIR is not a directory"
fi

# Secrets must never land in a repository checkout, and an uncertain answer
# counts as "yes". Two independent checks, both fail closed:
#  1. every ancestor of the state directory is searchable, and none holds a
#     checkout's .git: the file or link a worktree or submodule uses, or a
#     directory with a HEAD (git's own test). A .git directory that cannot be
#     read is a refusal; a readable one without HEAD is not a repository.
#     This needs no git at all;
#  2. when git is installed, it must positively say "not a git repository";
#     any other failure (dubious ownership, a broken repo) is a refusal.
in_repo() { die "TALOS_STATE_DIR $TALOS_STATE_DIR is $1; keep cluster secrets out of repositories"; }
probe=$TALOS_STATE_DIR
while [ ! -e "$probe" ]; do probe=$(dirname "$probe"); done
[ -d "$probe" ] || die "TALOS_STATE_DIR $TALOS_STATE_DIR: $probe is not a directory"
dir=$probe
while :; do
    [ -x "$dir" ] || die "cannot inspect $dir for a git checkout (not searchable); refusing to place secrets below it"
    git_dir=$dir/.git
    if [ -L "$git_dir" ] || { [ -e "$git_dir" ] && [ ! -d "$git_dir" ]; }; then
        in_repo "inside a git checkout ($git_dir)"
    elif [ -d "$git_dir" ]; then
        [ -r "$git_dir" ] && [ -x "$git_dir" ] \
            || die "cannot inspect $git_dir; refusing to place secrets below it"
        [ ! -e "$git_dir/HEAD" ] || in_repo "inside a git checkout ($git_dir)"
    fi
    [ "$dir" = / ] && break
    dir=$(dirname "$dir")
done
if command -v git >/dev/null 2>&1; then
    if answer=$(LC_ALL=C git -C "$probe" rev-parse --is-inside-work-tree 2>&1); then
        in_repo "inside a git repository (git rev-parse: $answer)"
    fi
    case "$answer" in
        *"not a git repository"*) ;;
        *) die "cannot tell whether $probe is in a git repository (git rev-parse: ${answer:-no output}); refusing to place secrets there" ;;
    esac
fi

[ -n "$NODES_FILE" ] || die "--nodes is required"
[ -r "$NODES_FILE" ] || die "cannot read node table $NODES_FILE"
NAMES=(); ROLES=(); IPS=(); MACS=()
n_cp=0; n_worker=0
while read -r name role ip mac extra; do
    case "$name" in ''|'#'*) continue ;; esac
    [ -z "${extra:-}" ] && [ -n "${mac:-}" ] || die "node table: expected '<name> <role> <ip> <mac>', got: $name $role $ip $mac $extra"
    [[ $name =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die "node table: bad name $name"
    [[ $ip =~ $IPV4 ]] || die "node table: $name has a bad address $ip"
    [[ $mac =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]] || die "node table: $name has a bad mac $mac"
    case "$role" in
        controlplane) n_cp=$((n_cp + 1)) ;;
        worker)       n_worker=$((n_worker + 1)) ;;
        *) die "node table: $name has unknown role $role" ;;
    esac
    [ "$ip" != "$TALOS_VIP" ] || die "node table: $name uses the VIP $TALOS_VIP as its address"
    for i in "${!NAMES[@]}"; do
        [ "${NAMES[$i]}" != "$name" ] || die "node table: duplicate name $name"
        [ "${IPS[$i]}" != "$ip" ]     || die "node table: duplicate address $ip"
        [ "${MACS[$i]}" != "$mac" ]   || die "node table: duplicate mac $mac"
    done
    NAMES+=("$name"); ROLES+=("$role"); IPS+=("$ip"); MACS+=("$mac")
done < "$NODES_FILE"
case "$n_cp+$n_worker" in
    3+3|1+1) ;;
    *) die "node table: need 3 control planes + 3 workers (or the 1+1 smoke profile), got $n_cp+$n_worker" ;;
esac
CP_IPS=()
for i in "${!NAMES[@]}"; do
    [ "${ROLES[$i]}" = controlplane ] && CP_IPS+=("${IPS[$i]}")
done

ENDPOINT="https://$TALOS_VIP:6443"
STATE=$TALOS_STATE_DIR
SECRETS=$STATE/secrets.yaml
IDENTITY="TALOS_CLUSTER_NAME=$TALOS_CLUSTER_NAME
KUBERNETES_ENDPOINT=$ENDPOINT
TALOS_VERSION=$TALOS_VERSION
KUBERNETES_VERSION=$KUBERNETES_VERSION"

# ---- dry run: the plan, nothing else ---------------------------------------
if [ "$DRY_RUN" = 1 ]; then
    echo "plan: cluster $TALOS_CLUSTER_NAME, endpoint $ENDPOINT, $n_cp control plane(s) + $n_worker worker(s)"
    echo "plan: state $STATE"
    if [ -e "$SECRETS" ]; then
        echo "plan: reuse $SECRETS"
    else
        echo "plan: talosctl gen secrets --talos-version $TALOS_VERSION -o $SECRETS"
    fi
    echo "plan: talosctl gen config $TALOS_CLUSTER_NAME $ENDPOINT --with-secrets $SECRETS" \
         "--talos-version $TALOS_VERSION --kubernetes-version $KUBERNETES_VERSION" \
         "--install-disk $TALOS_INSTALL_DISK --install-image $TALOS_INSTALLER_IMAGE"
    for i in "${!NAMES[@]}"; do
        echo "plan: ${NAMES[$i]} ${ROLES[$i]} ${IPS[$i]} ${MACS[$i]} -> $STATE/nodes/${NAMES[$i]}.yaml"
    done
    exit 0
fi

# ---- generate ---------------------------------------------------------------
command -v talosctl >/dev/null 2>&1 || die "talosctl is not on PATH"
have=$(talosctl version --client --short 2>/dev/null | awk '/Talos/ {print $2; exit}')
[ "$have" = "$TALOS_VERSION" ] || die "talosctl is ${have:-unknown}, inputs pin $TALOS_VERSION"
CHECK=$(dirname "$0")/check-node-config.py
[ -r "$CHECK" ] || die "missing $CHECK (ship it beside this script)"
python3 -c 'import yaml' 2>/dev/null || die "python3 with PyYAML is required to check merged configs"

run() { log "+ $*"; "$@"; }

mkdir -p "$STATE"
chmod 0700 "$STATE"
exec 9>"$STATE/.lock"
flock -n 9 || die "another gen-config run holds $STATE/.lock"

# Identity first: a lost bundle or changed identity inputs must stop the run,
# never quietly mint a second cluster identity.
if [ -e "$STATE/cluster.env" ]; then
    [ "$(cat "$STATE/cluster.env")" = "$IDENTITY" ] \
        || die "inputs differ from $STATE/cluster.env (cluster name, endpoint or versions); changing them is not supported by this script"
fi
if [ -e "$STATE/secrets.sha256" ] && [ ! -e "$SECRETS" ]; then
    die "$SECRETS is missing but was created before; restore it from backup -- a new bundle would be a different cluster"
fi
# A new bundle only for an empty state directory: anything a previous run
# left (identity, talosconfig, configs, patches) means this cluster exists
# and its bundle is lost, however much of the rest survived.
if [ ! -e "$SECRETS" ]; then
    leftovers=()
    shopt -s nullglob dotglob
    for entry in "$STATE"/*; do
        case "${entry##*/}" in .lock|.staging.*) ;; *) leftovers+=("${entry##*/}") ;; esac
    done
    shopt -u nullglob dotglob
    [ ${#leftovers[@]} -eq 0 ] \
        || die "$SECRETS is missing but $STATE holds earlier state (${leftovers[*]}); restore the bundle from backup, or start a new cluster in an empty directory"
fi
if [ -e "$SECRETS" ] && [ ! -e "$STATE/secrets.sha256" ]; then
    die "$SECRETS has no recorded fingerprint; refusing to guess whether it belongs to this cluster"
fi
if [ -e "$SECRETS" ] && [ ! -e "$STATE/cluster.env" ]; then
    die "$SECRETS exists without $STATE/cluster.env; refusing to guess which inputs it belongs to"
fi

STAGING=$(mktemp -d "$STATE/.staging.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT

if [ -e "$SECRETS" ]; then
    (cd "$STATE" && sha256sum --quiet -c secrets.sha256) \
        || die "$SECRETS does not match its recorded fingerprint"
    log "reusing $SECRETS"
else
    run talosctl gen secrets --talos-version "$TALOS_VERSION" -o "$STAGING/secrets.yaml"
    chmod 0600 "$STAGING/secrets.yaml"
    mv "$STAGING/secrets.yaml" "$SECRETS"
    (cd "$STATE" && sha256sum secrets.yaml > secrets.sha256)
    printf '%s\n' "$IDENTITY" > "$STATE/cluster.env"
    log "created $SECRETS"
fi

# settings shared by every node
{
    echo "machine:"
    echo "  time:"
    echo "    servers:"
    for s in $TALOS_NTP_SERVERS; do echo "      - \"$s\""; done
    echo "cluster:"
    echo "  network:"
    echo "    podSubnets: [\"$TALOS_POD_CIDR\"]"
    echo "    serviceSubnets: [\"$TALOS_SERVICE_CIDR\"]"
} > "$STAGING/cluster-patch.yaml"

run talosctl gen config "$TALOS_CLUSTER_NAME" "$ENDPOINT" \
    --with-secrets "$SECRETS" \
    --talos-version "$TALOS_VERSION" \
    --kubernetes-version "$KUBERNETES_VERSION" \
    --install-disk "$TALOS_INSTALL_DISK" \
    --install-image "$TALOS_INSTALLER_IMAGE" \
    --config-patch "@$STAGING/cluster-patch.yaml" \
    --output-types controlplane,worker,talosconfig \
    --output "$STAGING/gen"

mkdir -p "$STAGING/patches" "$STAGING/nodes"
for i in "${!NAMES[@]}"; do
    name=${NAMES[$i]} role=${ROLES[$i]}
    patch=$STAGING/patches/$name.yaml
    {
        echo "machine:"
        echo "  network:"
        echo "    hostname: $name"
        echo "    interfaces:"
        echo "      - deviceSelector:"
        echo "          hardwareAddr: \"${MACS[$i]}\""
        echo "        dhcp: true"
        if [ "$role" = controlplane ]; then
            echo "        vip:"
            echo "          ip: $TALOS_VIP"
        fi
    } > "$patch"
    args=()
    for extra in all "$role" "$name"; do
        f=${TALOS_PATCH_DIR:-}/$extra.yaml
        [ -n "${TALOS_PATCH_DIR:-}" ] && [ -f "$f" ] && args+=(--patch "@$f")
    done
    args+=(--patch "@$patch")
    run talosctl machineconfig patch "$STAGING/gen/$role.yaml" "${args[@]}" \
        --output "$STAGING/nodes/$name.yaml"
    # what patches may not change: identity, role, install inputs, node identity
    run python3 "$CHECK" "$STAGING/gen/$role.yaml" "$STAGING/nodes/$name.yaml" \
        "$name" "$role" "${MACS[$i]}" "$TALOS_VIP" \
        || die "$name: the merged config breaks a protected setting (see above); nothing was published"
    run talosctl validate --config "$STAGING/nodes/$name.yaml" --mode metal --strict
done

if [ ! -e "$STATE/talosconfig" ]; then
    run talosctl --talosconfig "$STAGING/gen/talosconfig" config endpoint "${CP_IPS[@]}"
    run talosctl --talosconfig "$STAGING/gen/talosconfig" config node "${CP_IPS[0]}"
    chmod 0600 "$STAGING/gen/talosconfig"
    mv "$STAGING/gen/talosconfig" "$STATE/talosconfig"
    log "created $STATE/talosconfig"
else
    log "keeping $STATE/talosconfig"
fi

# publish: report per node, replace only what changed
mkdir -p "$STATE/patches" "$STATE/nodes"
for name in "${NAMES[@]}"; do
    for kind in patches nodes; do
        new=$STAGING/$kind/$name.yaml cur=$STATE/$kind/$name.yaml
        chmod 0600 "$new"
        if [ ! -e "$cur" ]; then
            status=created
        elif cmp -s "$new" "$cur"; then
            status=unchanged
        else
            status=changed
        fi
        [ "$status" = unchanged ] || mv "$new" "$cur"
        [ "$kind" = nodes ] && echo "$name: $status"
    done
done
(cd "$STATE/nodes" && sha256sum -- "${NAMES[@]/%/.yaml}" > SHA256SUMS)
log "done: ${#NAMES[@]} node configs in $STATE/nodes"
