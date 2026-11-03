#!/usr/bin/env bash
# Check everything the install driver needs, before it changes anything.
#
#   preflight.sh [--inputs FILE]
#
# Runs on the host. Reads only: the rendered conf.yml, the version set, the
# private inputs, libvirt (domains, networks, leases), /proc/meminfo, df and
# the host's addresses and routes. Creates nothing, starts nothing, never
# calls boxman up. install.sh runs it first and stops if it fails.
#
# Checks, each failure reported with what to do about it:
#   inputs      every required input set, none still a PLACEHOLDER
#   versions    the pins well formed and one consistent tested set
#   topology    libvirt with the local runtime; exactly one control VM; the
#               talos group is 3 control planes + 3 workers, or 1+1 with the
#               smoke profile chosen explicitly (TALOS_PROFILE=smoke)
#   ownership   a first run finds none of this project's domains or its
#               network, and no earlier identity in the backup; a later run
#               finds exactly the domains (by UUID) it recorded
#   capacity    MemAvailable covers the guests not yet running plus
#               TALOS_RAM_HEADROOM_MIB (default 4096); the filesystems
#               holding the workspace and the template have room for the
#               virtual size of every disk not yet created
#   network     the fabric subnet overlaps no other libvirt network, host
#               address or route; the VIP lies inside it
#   access      the operator key is private, needs no passphrase and matches
#               TALOS_OPERATOR_PUBKEY; the backup directory is outside the
#               workspace and outside any git checkout
#   flux        TALOS_FLUX=on needs GITLAB_HOST, GITLAB_OWNER,
#               GITOPS_REPOSITORY, GITOPS_BRANCH, GITOPS_CLUSTER_PATH and
#               GITLAB_TOKEN; TALOS_FLUX=off (the default) skips Flux
#
# Exit status: 0 ready, 1 a check failed (nothing was changed).
set -euo pipefail
PROG=preflight
# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

INPUTS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --inputs) INPUTS=${2:?--inputs needs a file}; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

PROBLEMS=()
problem() { PROBLEMS+=("$*"); log "FAIL: $*"; }
ok() { log "ok: $*"; }

load_inputs "$INPUTS"

# ---- inputs ----------------------------------------------------------------
REQUIRED="TALOS_LAB_DIR TALOS_LAB_PREFIX TALOS_OPERATOR_KEY TALOS_OPERATOR_PUBKEY
TALOS_BACKUP_DIR TALOS_STATE_DIR TALOS_CLUSTER_NAME TALOS_INSTALL_DISK TALOS_VIP
TALOS_NTP_SERVERS TALOS_POD_CIDR TALOS_SERVICE_CIDR"
missing=0
for var in $REQUIRED; do
    val=${!var:-}
    if [ -z "$val" ]; then problem "$var is not set (see inputs.env.example)"; missing=1
    else case "$val" in *PLACEHOLDER*|*placeholder*) problem "$var is still a placeholder"; missing=1 ;; esac
    fi
done
PROFILE=${TALOS_PROFILE:-full}
case "$PROFILE" in full|smoke) ;; *) problem "TALOS_PROFILE must be full or smoke, got $PROFILE" ;; esac
for var in TALOS_LAB_DIR TALOS_BACKUP_DIR TALOS_STATE_DIR TALOS_OPERATOR_KEY; do
    case "${!var:-/}" in /*) ;; *) problem "$var must be an absolute path" ;; esac
done
FLUX=${TALOS_FLUX:-off}
# the fixed topology gives every node exactly one disk, which boxman creates
# and the host checks; nothing else may be installed on
[ -z "${TALOS_INSTALL_DISK:-}" ] || [ "$TALOS_INSTALL_DISK" = /dev/vda ] \
    || problem "TALOS_INSTALL_DISK must be /dev/vda, the one boot disk boxman creates for each node (got $TALOS_INSTALL_DISK)"
case "$FLUX" in on|off) ;; *) problem "TALOS_FLUX must be on or off, got $FLUX" ;; esac
case "${TALOS_LIBVIRT_USE_SUDO:-true}" in true|false) ;; *) problem "TALOS_LIBVIRT_USE_SUDO must be true or false" ;; esac
[ $missing = 0 ] && ok "inputs complete${INPUTS_FILE:+ ($INPUTS_FILE)}"

# ---- versions: one consistent, well-formed tested set ------------------------
for entry in "${PINNED[@]}"; do
    key=${entry%%=*}
    [ "${!key:-}" = "${entry#*=}" ] || log "note: $key=${!key:-} overrides the pinned ${entry#*=}"
done
HEX='^[0-9a-f]{64}$'
v_ok=1
vproblem() { problem "versions: $*"; v_ok=0; }
[[ ${TALOS_VERSION:-} =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || vproblem "TALOS_VERSION must be an exact release"
[[ ${KUBERNETES_VERSION:-} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || vproblem "KUBERNETES_VERSION must be an exact release"
[ "${KUBECTL_VERSION:-}" = "v${KUBERNETES_VERSION:-}" ] || vproblem "KUBECTL_VERSION must be v\$KUBERNETES_VERSION"
[[ ${FLUX_VERSION:-} =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || vproblem "FLUX_VERSION must be an exact release"
for var in TALOSCTL_SHA256 KUBECTL_SHA256 FLUX_SHA256 TALOS_SCHEMATIC_ID; do
    [[ ${!var:-} =~ $HEX ]] || vproblem "$var must be a sha256 (64 hex digits)"
done
[[ ${TALOS_ISO_CHECKSUM:-} =~ ^sha256:[0-9a-f]{64}$ ]] || vproblem "TALOS_ISO_CHECKSUM must be sha256:<64 hex digits>"
case "${TALOS_ISO_URI:-}" in
    https://*/"${TALOS_VERSION:-x}"/*.iso) ;;
    *) vproblem "TALOS_ISO_URI must be an https URL of the pinned Talos version's ISO" ;;
esac
[[ ${TALOS_INSTALLER_IMAGE:-} =~ ^[a-z0-9.-]+/[a-z0-9./-]*/${TALOS_SCHEMATIC_ID:-x}:${TALOS_VERSION:-x}@sha256:[0-9a-f]{64}$ ]] \
    || vproblem "TALOS_INSTALLER_IMAGE must be <registry>/<repo>/<schematic>:<version>@sha256:<digest> of the pinned set"
[ $v_ok = 1 ] && ok "versions: Talos $TALOS_VERSION, Kubernetes $KUBERNETES_VERSION, flux $FLUX_VERSION, all checksums pinned"

# ---- host tools ---------------------------------------------------------------
for tool in virsh ssh ssh-keygen tar flock sha256sum awk df ip "$PYTHON"; do
    command -v "$tool" >/dev/null 2>&1 || problem "$tool is not installed on this host"
done

if [ ${#PROBLEMS[@]} -gt 0 ]; then
    die "${#PROBLEMS[@]} problem(s); nothing was changed"
fi

# ---- topology -------------------------------------------------------------------
read_facts
case "$LIBVIRT_URI" in
    qemu:///system|qemu:///session) ;;
    *) problem "provider.libvirt.uri $LIBVIRT_URI: v0 supports a local libvirt only" ;;
esac
boxman_conf=${BOXMAN_APP_CONF:-$HOME/.config/boxman/boxman.yml}
if [ -r "$boxman_conf" ]; then
    runtime=$("$PYTHON" -c 'import sys, yaml; print((yaml.safe_load(open(sys.argv[1])) or {}).get("runtime") or "local")' "$boxman_conf") \
        || runtime=unreadable
    [ "$runtime" = local ] || problem "boxman runtime is $runtime ($boxman_conf); v0 supports the local runtime only"
fi
[ "$USE_SUDO" = "${TALOS_LIBVIRT_USE_SUDO:-true}" ] || problem "rendered use_sudo $USE_SUDO differs from TALOS_LIBVIRT_USE_SUDO"
n_control=0 n_cp=0 n_worker=0
for i in "${!VM_DOMAIN[@]}"; do
    case "${VM_ROLE[$i]}" in
        control) n_control=$((n_control + 1)) ;;
        controlplane) n_cp=$((n_cp + 1)) ;;
        worker) n_worker=$((n_worker + 1)) ;;
    esac
done
[ "$n_control" = 1 ] || problem "topology: need exactly one control VM, the conf has $n_control"
case "$PROFILE:$n_cp+$n_worker" in
    full:3+3|smoke:1+1) ok "topology: $PROFILE profile, 1 control VM + $n_cp control plane(s) + $n_worker worker(s), project $PROJECT" ;;
    *) problem "topology: the $PROFILE profile has $n_cp+$n_worker nodes; supported are 3+3 (full) and 1+1 (TALOS_PROFILE=smoke)" ;;
esac
below "$WORKSPACE" "$TALOS_LAB_DIR" || problem "workspace $WORKSPACE is not TALOS_LAB_DIR $TALOS_LAB_DIR"


# ---- ownership --------------------------------------------------------------------
# Existence comes from complete, checked listings: a failed query must not
# read as "absent" (and so as a free name).
ALL_DOMAINS=$(vsh list --all --name) || die "ownership: cannot list the libvirt domains; nothing was changed"
ALL_NETS=$(vsh net-list --all --name) || die "ownership: cannot list the libvirt networks; nothing was changed"
listed() { printf '%s\n' "$2" | grep -qxF -- "$1"; }
STATES=()
present=()
for i in "${!VM_DOMAIN[@]}"; do
    dom=${VM_DOMAIN[$i]}
    if listed "$dom" "$ALL_DOMAINS"; then
        state=$(vsh domstate "$dom" | sed -n '1{s/[[:space:]]*$//;p}') && [ -n "$state" ] \
            || die "ownership: cannot read the state of $dom; nothing was changed"
        present+=("$dom")
    else
        state=absent
    fi
    STATES+=("$state")
done
net_present=0
listed "$NETWORK" "$ALL_NETS" && net_present=1
template_present=0
listed "$TEMPLATE" "$ALL_DOMAINS" && template_present=1
# A domain or the network may be touched only with positive evidence that
# this driver created them: their UUIDs, which install.sh records all at once
# after `boxman up` succeeded on the run that created them (created=yes).
# Matching names prove nothing, and neither does a record whose `up` never
# finished: then nothing of the project may exist yet.
if [ ! -e "$RECORD" ] || [ "$(record_get created)" != yes ]; then
    if [ ${#present[@]} -gt 0 ] || [ $net_present = 1 ]; then
        found=${present[*]:-}
        [ $net_present = 0 ] || found="$found $NETWORK"
        if [ -e "$RECORD" ]; then
            why="boxman up never finished on the run that recorded $RECORD, so which of them it created cannot be proven"
        else
            why="this driver has no record of project $PROJECT ($RECORD)"
        fi
        problem "ownership: $why, but these exist:$found. A run never starts or adopts what it cannot prove it created; destroy them (boxman destroy) and start again, or pick another TALOS_PROJECT"
    elif [ -e "$RECORD" ]; then
        ok "ownership: project $PROJECT: an earlier boxman up created nothing; starting again"
    else
        ok "ownership: new project $PROJECT, none of its domains or its network exist"
    fi
    for dir in "$WORKSPACE/management" "$WORKSPACE/talos"; do
        if compgen -G "$dir/*.qcow2" >/dev/null; then
            problem "ownership: $dir already holds disks; a new run starts from an empty workspace"
        fi
    done
    if [ -e "$TALOS_BACKUP_DIR/state/secrets.yaml" ]; then
        problem "ownership: $TALOS_BACKUP_DIR already backs up a cluster identity; a new cluster needs its own TALOS_BACKUP_DIR"
    fi
else
    [ "$(record_get project)" = "$PROJECT" ] \
        || problem "ownership: $RECORD belongs to project $(record_get project), not $PROJECT"
    owned=1
    for i in "${!VM_DOMAIN[@]}"; do
        dom=${VM_DOMAIN[$i]} want=$(record_get "uuid.${VM_NAME[$i]}")
        if [ -z "$want" ]; then
            problem "ownership: $RECORD records no uuid for $dom; refusing to touch a domain this driver cannot prove it created"; owned=0
        elif [ "${STATES[$i]}" = absent ]; then
            problem "ownership: $dom was created by this driver (uuid $want) and is gone; v0 does not recreate a node -- destroy the project and start again"; owned=0
        elif ! uuid=$(vsh domuuid "$dom" | sed -n '1{s/[[:space:]]*$//;p}') || [ -z "$uuid" ]; then
            die "ownership: cannot read the uuid of $dom; nothing was changed"
        elif [ "$uuid" != "$want" ]; then
            problem "ownership: $dom is not the domain this driver created (uuid differs from $want); refusing to touch it"; owned=0
        fi
    done
    want=$(record_get net.uuid)
    if [ -z "$want" ]; then
        problem "ownership: $RECORD records no uuid for the network $NETWORK"; owned=0
    elif [ $net_present = 0 ]; then
        problem "ownership: the network $NETWORK this driver created (uuid $want) is gone; destroy the project and start again"; owned=0
    elif ! uuid=$(vsh net-uuid "$NETWORK" | sed -n '1{s/[[:space:]]*$//;p}') || [ -z "$uuid" ]; then
        die "ownership: cannot read the uuid of $NETWORK; nothing was changed"
    elif [ "$uuid" != "$want" ]; then
        problem "ownership: $NETWORK is not the network this driver created (uuid differs from $want); refusing to touch it"; owned=0
    fi
    [ $owned = 1 ] && ok "ownership: the domains and network of $PROJECT are the ones this driver created (by uuid)"
fi

# ---- capacity -----------------------------------------------------------------------
need_mib=0
need_disk=0
for i in "${!VM_DOMAIN[@]}"; do
    [ "${STATES[$i]}" = running ] || need_mib=$((need_mib + VM_MEM[i]))
    [ "${STATES[$i]}" != absent ] || need_disk=$((need_disk + VM_DISK[i]))
done
headroom=${TALOS_RAM_HEADROOM_MIB:-4096}
[[ $headroom =~ ^[0-9]+$ ]] || problem "TALOS_RAM_HEADROOM_MIB must be a number of MiB"
avail_kib=$(awk '/^MemAvailable:/ {print $2; exit}' "${TALOS_MEMINFO:-/proc/meminfo}") \
    && [[ $avail_kib =~ ^[0-9]+$ ]] || die "memory: cannot read MemAvailable; nothing was changed"
avail_mib=$((avail_kib / 1024))
if [ $((need_mib + headroom)) -le "$avail_mib" ]; then
    ok "memory: $need_mib MiB for guests not running + $headroom MiB headroom <= $avail_mib MiB available"
else
    problem "memory: $need_mib MiB for guests not running + $headroom MiB headroom > $avail_mib MiB available; free memory or use the smoke profile"
fi
# df_of PATH: "<device> <available KiB>" of the filesystem holding PATH
df_of() {
    local p=$1 out
    while [ ! -e "$p" ]; do p=$(dirname -- "$p"); done
    out=$(df -Pk -- "$p" | awk 'NR == 2 {print $1, $4}') && [[ $out =~ ^[^\ ]+\ [0-9]+$ ]] \
        || die "disk: cannot read the free space of $p; nothing was changed"
    printf '%s\n' "$out"
}
ws=$(df_of "$WORKSPACE") || exit 1
read -r ws_dev ws_avail <<< "$ws"
template_disk=0
[ $template_present = 1 ] || template_disk=$TEMPLATE_DISK
tp=$(df_of "$TEMPLATE_DIR") || exit 1
read -r tp_dev tp_avail <<< "$tp"
if [ "$ws_dev" = "$tp_dev" ]; then
    need_disk=$((need_disk + template_disk)); template_disk=0
fi
gib() { echo $(( ($1 + (1 << 30) - 1) >> 30 )); }
if [ $((need_disk / 1024)) -le "${ws_avail:-0}" ]; then
    ok "disk: $(gib $need_disk) GiB of virtual disks to create <= $(( ${ws_avail:-0} >> 20 )) GiB free for $WORKSPACE"
else
    problem "disk: $(gib $need_disk) GiB of virtual disks to create > $(( ${ws_avail:-0} >> 20 )) GiB free for $WORKSPACE"
fi
if [ "$template_disk" -gt 0 ] && [ $((template_disk / 1024)) -gt "${tp_avail:-0}" ]; then
    problem "disk: the template's $(gib "$template_disk") GiB disk does not fit in $TEMPLATE_DIR"
fi

# ---- network --------------------------------------------------------------------------
# Every observation is captured with its exit status checked, and every line
# of it parsed strictly: a listing that failed, or one that cannot be read
# completely, stops the run. A partial inventory must never read as "no
# overlap" on a shared host.
inventory_fail() { die "network: cannot $*; refusing to judge overlaps on a partial inventory (nothing was changed)"; }
IPV4_RE='^((25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])$'
ip_int() { local a b c d; IFS=. read -r a b c d <<< "$1"; echo $(( (a << 24) | (b << 16) | (c << 8) | d )); }
mask_len() { local m; m=$(ip_int "$1"); local n=0; while [ $((m & 0x80000000)) -ne 0 ]; do n=$((n + 1)); m=$(((m << 1) & 0xFFFFFFFF)); done; echo $n; }
len_mask() { echo $(( $1 == 0 ? 0 : (0xFFFFFFFF << (32 - $1)) & 0xFFFFFFFF )); }
# overlap A LEN B LEN: the two IPv4 prefixes share an address
overlap() {
    local m; m=$(len_mask $(( $2 < $4 ? $2 : $4 )))
    [ $(( $(ip_int "$1") & m )) -eq $(( $(ip_int "$3") & m )) ]
}
# cidr_ok ADDR LEN: a well-formed IPv4 prefix
cidr_ok() { [[ $1 =~ $IPV4_RE ]] && [[ $2 =~ ^([0-9]|[12][0-9]|3[0-2])$ ]]; }
# net_xml_facts: "bridge <name>" and "ip <address> <prefix length>" lines from
# a libvirt network's XML on stdin; any malformed or unreadable element fails
NET_XML_FACTS='
import sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read())
if root.tag != "network":
    sys.exit("not a libvirt network document")
bridge = root.find("bridge")
if bridge is not None and bridge.get("name"):
    print("bridge", bridge.get("name"))
for ip in root.findall("ip"):
    if ip.get("family", "ipv4") != "ipv4":
        continue
    addr = ip.get("address")
    if not addr:
        sys.exit("an <ip> element without an address")
    if ip.get("prefix") is not None:
        length = int(ip.get("prefix"))
    elif ip.get("netmask"):
        octets = [int(p) for p in ip.get("netmask").split(".")]
        bits = "".join(format(o, "08b") for o in octets)
        if len(octets) != 4 or "01" in bits:
            sys.exit("bad netmask " + ip.get("netmask"))
        length = bits.count("1")
    else:
        length = 24 if int(addr.split(".")[0]) >= 192 else 16 if int(addr.split(".")[0]) >= 128 else 8
    print("ip", addr, length)
'
FAB_LEN=$(mask_len "$NET_MASK")
net_ok=1
our_bridge=""
if [ $net_present = 1 ]; then
    xml=$(vsh net-dumpxml "$NETWORK") || inventory_fail "read this project's network $NETWORK"
    facts=$(printf '%s' "$xml" | "$PYTHON" -c "$NET_XML_FACTS") || inventory_fail "parse this project's network $NETWORK"
    our_bridge=$(printf '%s\n' "$facts" | awk '$1 == "bridge" {print $2; exit}')
    [ -n "$our_bridge" ] || inventory_fail "find the bridge of this project's network $NETWORK"
fi
n_nets=0
while read -r other; do
    [ -n "$other" ] && [ "$other" != "$NETWORK" ] || continue
    xml=$(vsh net-dumpxml "$other") || inventory_fail "read the libvirt network $other"
    facts=$(printf '%s' "$xml" | "$PYTHON" -c "$NET_XML_FACTS") || inventory_fail "parse the libvirt network $other"
    n_nets=$((n_nets + 1))
    while read -r kind addr len; do
        [ "$kind" = ip ] || continue
        cidr_ok "$addr" "$len" || inventory_fail "parse the address $addr/$len of libvirt network $other"
        if overlap "$NET_ADDR" "$FAB_LEN" "$addr" "$len"; then
            problem "network: fabric $NET_ADDR/$FAB_LEN overlaps libvirt network $other ($addr/$len); set TALOS_LAB_PREFIX to a free /24"; net_ok=0
        fi
    done <<< "$facts"
done <<< "$ALL_NETS"
addrs=$(ip -4 -o addr show) || inventory_fail "list the host's IPv4 addresses (ip -4 -o addr show)"
n_addrs=0
while read -r line; do
    [ -n "$line" ] || continue
    read -r _ dev family cidr _ <<< "$line"
    [ "${family:-}" = inet ] && cidr_ok "${cidr%/*}" "${cidr#*/}" && [ "${cidr%/*}" != "$cidr" ] \
        || inventory_fail "parse the host address line: $line"
    n_addrs=$((n_addrs + 1))
    [ "$dev" != "$our_bridge" ] || continue
    if overlap "$NET_ADDR" "$FAB_LEN" "${cidr%/*}" "${cidr#*/}"; then
        problem "network: fabric $NET_ADDR/$FAB_LEN overlaps $cidr on host interface $dev"; net_ok=0
    fi
done <<< "$addrs"
[ $n_addrs -gt 0 ] || inventory_fail "find any host IPv4 address (not even loopback)"
routes=$(ip -4 route show) || inventory_fail "list the host's IPv4 routes (ip -4 route show)"
while read -r line; do
    [ -n "$line" ] || continue
    set -f
    # shellcheck disable=SC2086  # split the route into its words, no globbing
    set -- $line
    set +f
    case "$1" in
        unicast|local|broadcast|multicast|throw|unreachable|prohibit|blackhole|nat) shift ;;
    esac
    dst=$1 dev=""
    while [ $# -gt 1 ]; do [ "$1" = dev ] && dev=$2; shift; done
    [ "$dst" != default ] || continue
    case "$dst" in */*) ;; *) dst=$dst/32 ;; esac
    cidr_ok "${dst%/*}" "${dst#*/}" || inventory_fail "parse the host route: $line"
    [ -z "$our_bridge" ] || [ "$dev" != "$our_bridge" ] || continue
    if overlap "$NET_ADDR" "$FAB_LEN" "${dst%/*}" "${dst#*/}"; then
        problem "network: fabric $NET_ADDR/$FAB_LEN overlaps the host route $dst${dev:+ (dev $dev)}"; net_ok=0
    fi
done <<< "$routes"
if [[ ${TALOS_VIP:-} =~ $IPV4_RE ]] && overlap "$TALOS_VIP" 32 "$NET_ADDR" "$FAB_LEN"; then
    [ "$TALOS_VIP" != "$NET_ADDR" ] || { problem "network: the VIP is the fabric gateway"; net_ok=0; }
else
    problem "network: TALOS_VIP ${TALOS_VIP:-} is not inside the fabric $NET_ADDR/$FAB_LEN"; net_ok=0
fi
[ $net_ok = 1 ] && ok "network: fabric $NET_ADDR/$FAB_LEN overlaps none of $n_nets other libvirt networks, $n_addrs host addresses or the host routes; VIP $TALOS_VIP inside it"

# ---- access -------------------------------------------------------------------------------
key=$TALOS_OPERATOR_KEY
if [ ! -r "$key" ]; then
    problem "access: cannot read the operator key $key"
elif [ -n "$(find "$key" -maxdepth 0 -perm /077 2>/dev/null)" ]; then
    problem "access: the operator key $key is readable by others (chmod 600)"
elif ! pub=$(ssh-keygen -y -P '' -f "$key" 2>/dev/null); then
    problem "access: the operator key $key needs a passphrase or is not a key; the driver runs without a prompt"
else
    read -r want_type want_key _ <<< "$TALOS_OPERATOR_PUBKEY"
    read -r have_type have_key _ <<< "$pub"
    if [ "$want_type $want_key" = "$have_type $have_key" ]; then
        ok "access: operator key $key matches TALOS_OPERATOR_PUBKEY"
    else
        problem "access: TALOS_OPERATOR_PUBKEY is not the public half of $key; the control template would authorize another key"
    fi
fi
# The backup must survive a destroy of the workspace: compare where the data
# would physically land, symlinks resolved on both sides. install.sh writes
# only to this checked physical path.
# The driver's own directory (default <workspace>/driver) receives the
# kubeconfig and, transiently, the state archive: it must not be, or be able
# to be, in a git checkout either, by its physical path.
if ! driver_phys=$(physical_path "$DRIVER_DIR" 2>&1); then
    problem "access: the driver's directory: $driver_phys"
elif below "$driver_phys" "$BOX_DIR"; then
    problem "access: the driver's directory $driver_phys is inside the box; set TALOS_LAB_DIR (or TALOS_DRIVER_DIR) outside the repository"
else
    ( outside_checkouts "$driver_phys" "the driver's directory" ) || problem "access: see above (the driver's directory $DRIVER_DIR)"
fi
if [ -n "${TALOS_BACKUP_DIR:-}" ]; then
    if ! backup_phys=$(physical_path "$TALOS_BACKUP_DIR" 2>&1); then
        problem "access: TALOS_BACKUP_DIR: $backup_phys"
    elif ! ws_phys=$(physical_path "$WORKSPACE" 2>&1); then
        problem "access: the workspace: $ws_phys"
    elif below "$backup_phys" "$ws_phys" || below "$backup_phys" "$BOX_DIR"; then
        problem "access: TALOS_BACKUP_DIR $TALOS_BACKUP_DIR is $backup_phys, inside the workspace ($ws_phys) or the box; it must be outside both, so a destroy cannot take the backup"
    elif below "$ws_phys" "$backup_phys"; then
        problem "access: the workspace $ws_phys lies inside TALOS_BACKUP_DIR $backup_phys; keep them apart"
    else
        ( outside_checkouts "$TALOS_BACKUP_DIR" TALOS_BACKUP_DIR ) || problem "access: see above (TALOS_BACKUP_DIR)"
        probe=$TALOS_BACKUP_DIR
        while [ ! -e "$probe" ]; do probe=$(dirname -- "$probe"); done
        [ -w "$probe" ] || problem "access: cannot write $probe for TALOS_BACKUP_DIR"
    fi
fi

# ---- flux ------------------------------------------------------------------------------------
if [ "$FLUX" = on ]; then
    f_ok=1
    for var in GITLAB_HOST GITLAB_OWNER GITOPS_REPOSITORY GITOPS_BRANCH GITOPS_CLUSTER_PATH GITLAB_TOKEN; do
        val=${!var:-}
        if [ -z "$val" ]; then problem "flux: TALOS_FLUX=on needs $var"; f_ok=0
        else case "$val" in *PLACEHOLDER*|*placeholder*) problem "flux: $var is still a placeholder"; f_ok=0 ;; esac
        fi
    done
    [[ ${GITLAB_HOST:-x} =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || { problem "flux: GITLAB_HOST must be a host name"; f_ok=0; }
    [[ ${GITLAB_OWNER:-x} =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] || { problem "flux: GITLAB_OWNER must be a GitLab user or group path"; f_ok=0; }
    [[ ${GITOPS_REPOSITORY:-x} =~ ^[A-Za-z0-9._-]+$ ]] || { problem "flux: GITOPS_REPOSITORY must be a project name"; f_ok=0; }
    [[ ${GITOPS_BRANCH:-x} =~ ^[A-Za-z0-9._/-]+$ ]] && [[ ${GITOPS_BRANCH:-x} != *..* ]] \
        || { problem "flux: GITOPS_BRANCH is not a plain branch name"; f_ok=0; }
    case "${GITOPS_CLUSTER_PATH:-x}" in
        /*|*..*|.|./) problem "flux: GITOPS_CLUSTER_PATH must be a relative path inside the repository, such as ./clusters/<name>"; f_ok=0 ;;
    esac
    case "${GITLAB_PERSONAL:-false}" in true|false) ;; *) problem "flux: GITLAB_PERSONAL must be true or false"; f_ok=0 ;; esac
    [ $f_ok = 1 ] && ok "flux: on, $GITLAB_HOST/$GITLAB_OWNER/$GITOPS_REPOSITORY $GITOPS_BRANCH $GITOPS_CLUSTER_PATH (credential supplied)"
else
    ok "flux: off (TALOS_FLUX=on with a GitOps target and credential enables it)"
fi

if [ ${#PROBLEMS[@]} -gt 0 ]; then
    die "${#PROBLEMS[@]} problem(s); nothing was changed"
fi
log "ready: project $PROJECT, $PROFILE profile"
