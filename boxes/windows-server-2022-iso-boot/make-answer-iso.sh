#!/usr/bin/env bash
# Build the answer ISO (autounattend.xml + boxman-setup.ps1 [+ authorized_keys])
# that conf.yml attaches as the third CD-ROM, install-only.
#
#   BOXMAN_WIN_ADMIN_PASS=...  required; Administrator password. Must meet
#                              Windows' complexity rule (8+ chars, 3 of:
#                              upper, lower, digit, symbol) or OOBE stops.
#   BOXMAN_WIN_SSH_PUBKEY=...  optional; path of a public key to authorize
#                              for Administrator over OpenSSH.
#   BOXMAN_WIN_HOSTNAME=...    optional; computer name (default win01).
#
# Usage: ./make-answer-iso.sh [output.iso]
#        (default: ~/workspaces/windows-server-2022-iso-boot/answer.iso,
#         the path conf.yml expects)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$HOME/workspaces/windows-server-2022-iso-boot/answer.iso}
pass=${BOXMAN_WIN_ADMIN_PASS:?set BOXMAN_WIN_ADMIN_PASS to the Administrator password}
name=${BOXMAN_WIN_HOSTNAME:-win01}

classes=0
[[ $pass =~ [A-Z] ]] && classes=$((classes + 1))
[[ $pass =~ [a-z] ]] && classes=$((classes + 1))
[[ $pass =~ [0-9] ]] && classes=$((classes + 1))
[[ $pass =~ [^A-Za-z0-9] ]] && classes=$((classes + 1))
if (( ${#pass} < 8 || classes < 3 )); then
    echo "BOXMAN_WIN_ADMIN_PASS does not meet Windows' complexity rule" >&2
    exit 2
fi
# NetBIOS computer name: 1-15 chars, letters/digits/hyphen, not all digits
if ! [[ $name =~ ^[A-Za-z0-9-]{1,15}$ ]] || [[ $name =~ ^[0-9]+$ ]]; then
    echo "BOXMAN_WIN_HOSTNAME '$name' is not a valid Windows computer name" >&2
    exit 2
fi
# genisoimage, mkisofs and xorrisofs take the same flags; distros ship
# different ones (Debian's xorriso package has only xorrisofs).
for iso_tool in genisoimage mkisofs xorrisofs; do
    command -v "$iso_tool" >/dev/null && break
done
if ! command -v "$iso_tool" >/dev/null; then
    echo "need genisoimage, mkisofs or xorrisofs to build the answer ISO" >&2
    exit 2
fi

# Created before the umask below, so qemu can still reach the VM disk there.
mkdir -p "$(dirname "$out")"
umask 077
stage=$(mktemp -d)
trap 'rm -rf "$stage" "$out.tmp"' EXIT

# The password is escaped in Python: bash 5.2 expands an unquoted & in a
# ${s//pat/rep} replacement to the matched text, which garbled the entities.
PASS=$pass NAME=$name python3 - "$here/autounattend.xml.in" \
    "$stage/autounattend.xml" <<'EOF'
import os, sys
from xml.sax.saxutils import escape
text = open(sys.argv[1], encoding="utf-8").read()
text = text.replace("@ADMIN_PASS@", escape(os.environ["PASS"]))
text = text.replace("@COMPUTER_NAME@", os.environ["NAME"])
open(sys.argv[2], "w", encoding="utf-8").write(text)
EOF
# Setup rejects a malformed answer file only at boot, minutes in; catch it here.
if command -v xmllint >/dev/null; then
    xmllint --noout "$stage/autounattend.xml"
fi
cp "$here/boxman-setup.ps1" "$stage/"
if [[ -n ${BOXMAN_WIN_SSH_PUBKEY:-} ]]; then
    cp "$BOXMAN_WIN_SSH_PUBKEY" "$stage/authorized_keys"
fi

# Write beside the target and rename, so a guest never sees a half-written ISO.
"$iso_tool" -quiet -J -r -V BOXMAN_ANSWER -o "$out.tmp" "$stage"
mv -f "$out.tmp" "$out"
echo "wrote $out"
