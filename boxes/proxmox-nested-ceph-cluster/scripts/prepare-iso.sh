#!/usr/bin/env bash
# Build the generic Proxmox VE auto-install ISO on this host:
#   1. download + sha256-verify the upstream ISO into $LAB/iso (cached)
#   2. in a throwaway debian:trixie container, install
#      proxmox-auto-install-assistant from the PVE no-subscription repo,
#      validate answer.toml and bake it + first-boot.sh into the ISO
#   3. place the result at $AUTO_ISO (what conf.yml's cdroms[].source points at)
# Idempotent; re-run to rebuild after editing answer.toml or first-boot.sh.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANSWER="$BOX/answer.toml"
[[ -f $ANSWER ]] || die "$ANSWER missing: run 'make answer' on the workstation (renders answer.toml.tmpl)"
grep -q '__PUBKEY__\|__PWHASH__' "$ANSWER" && die "$ANSWER still has template placeholders"

# 1. upstream ISO
mkdir -p "$LAB/iso"
cd "$LAB/iso"
if [[ ! -f $ISO_NAME ]] || ! echo "$ISO_SHA256  $ISO_NAME" | sha256sum -c --quiet 2>/dev/null; then
    log "downloading $ISO_URL"
    rm -f "$ISO_NAME"
    curl -fL --retry 3 -o "$ISO_NAME.part" "$ISO_URL"
    mv "$ISO_NAME.part" "$ISO_NAME"
    echo "$ISO_SHA256  $ISO_NAME" | sha256sum -c
else
    log "$ISO_NAME present, checksum ok"
fi

# 2. bake the answer file + first-boot hook
build="$LAB/build"
rm -rf "$build"; mkdir -p "$build"
cp "$ANSWER" "$build/answer.toml"
cp "$BOX/scripts/first-boot.sh" "$build/first-boot.sh"
# the assistant copies the source ISO to a temp file *next to it*, so give it
# a writable copy: a hardlink (same filesystem) costs nothing, cp is the fallback
ln "$LAB/iso/$ISO_NAME" "$build/$ISO_NAME" 2>/dev/null || cp "$LAB/iso/$ISO_NAME" "$build/$ISO_NAME"
log "preparing the auto-install ISO in a debian:trixie container"
docker run --rm \
    -v "$build:/build" \
    -e ISO_NAME="$ISO_NAME" -e OWNER="$(id -u):$(id -g)" \
    debian:trixie bash -euo pipefail -c '
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends ca-certificates curl xorriso >/dev/null
        curl -fsSL https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
            -o /usr/share/keyrings/proxmox-archive-keyring.gpg
        printf "Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n" \
            > /etc/apt/sources.list.d/pve.sources
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends proxmox-auto-install-assistant >/dev/null
        proxmox-auto-install-assistant --version
        proxmox-auto-install-assistant validate-answer /build/answer.toml
        proxmox-auto-install-assistant prepare-iso "/build/$ISO_NAME" \
            --fetch-from iso --answer-file /build/answer.toml \
            --on-first-boot /build/first-boot.sh --output /build/auto.iso
        chown "$OWNER" /build/auto.iso
    ' 2>&1 | tee "$LOGS/prepare-iso.log" | grep -v '^$' | tail -20

[[ -s $build/auto.iso ]] || die "no ISO produced; see $LOGS/prepare-iso.log"
mv -f "$build/auto.iso" "$AUTO_ISO"
log "auto-install ISO ready: $AUTO_ISO ($(du -h "$AUTO_ISO" | cut -f1))"
