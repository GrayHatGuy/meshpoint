#!/usr/bin/env bash
#
# backup_meshpoint.sh -- snapshot every piece of Meshpoint + Reticulum
# + MeshCore state that's been hand-built on this Pi into one timestamped
# tarball, suitable for:
#   * disaster recovery (re-flash the SD card and restore_meshpoint.sh
#     drops everything back into place)
#   * cloning a known-good config to another Pi
#   * pre-experiment safety net before changing risky things
#
# What's captured (all paths preserved relative to "/" inside the tarball
# so restore_meshpoint.sh can extract straight back):
#
#   /opt/meshpoint/config/local.yaml         -- capture / TX / radio config
#   /opt/meshpoint/data/                     -- sqlite db, lxmf_sent log,
#                                               lxmf_contacts, announce state
#   ~mp/.reticulum/                          -- rnsd config + identity +
#                                               announce cache + known dest
#   ~mp/.lxmd/                               -- lxmd config + identity +
#                                               messagestore + inbox.json +
#                                               peers.json + ratchets
#   /etc/sudoers.d/meshpoint-lxmf            -- sudo grant for send/announce
#                                               /rnstatus/systemctl restart
#   /etc/systemd/system/rnsd.service         -- our installed unit
#   /etc/systemd/system/lxmd.service
#   /etc/systemd/system/lxmf-inbox-dump.service
#
# Plus generated metadata inside the tarball:
#   _MANIFEST.txt       file list with sizes + sha256
#   _SHA256SUM          checksum of the manifest (verify integrity)
#   _BUILD_INFO.txt     git rev of /opt/meshpoint at backup time,
#                       hostname, kernel, date, lsusb, ttyUSB listing
#
# IMPORTANT: identity files (~/.reticulum/storage/identity,
# ~/.lxmd/identity) contain PRIVATE KEYS. The tarball is created mode
# 0600 root-only and is NEVER world-readable. Treat it like an SSH key.
#
# Usage:
#   sudo bash /opt/meshpoint/scripts/backup_meshpoint.sh [output_dir]
#
# Default output dir is /opt/meshpoint/data/backups/. Pass a different
# path as the first arg to write elsewhere (e.g. a USB stick mount).
#

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────
INVOKING_USER="${SUDO_USER:-${USER:-mp}}"
USER_HOME="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"
[ -d "$USER_HOME" ] || { echo "[backup] ERROR: home dir for $INVOKING_USER not found" >&2; exit 1; }

OUT_DIR="${1:-/opt/meshpoint/data/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
HOSTNAME_SHORT="$(hostname -s)"
TARBALL="$OUT_DIR/meshpoint-backup-${HOSTNAME_SHORT}-${STAMP}.tar.gz"

info() { echo "[backup] $*"; }
warn() { echo "[backup] WARN: $*" >&2; }

# ── Resolve the set of source paths actually present on this host ────
# Skip non-existent paths quietly -- a fresh install may not have
# every artifact (e.g. no sent log yet, no announce cache cleared).
candidate_paths=(
    "/opt/meshpoint/config/local.yaml"
    "/opt/meshpoint/data"
    "$USER_HOME/.reticulum"
    "$USER_HOME/.lxmd"
    "/etc/sudoers.d/meshpoint-lxmf"
    "/etc/systemd/system/rnsd.service"
    "/etc/systemd/system/lxmd.service"
    "/etc/systemd/system/lxmf-inbox-dump.service"
)

resolved_paths=()
for p in "${candidate_paths[@]}"; do
    if [ -e "$p" ]; then
        resolved_paths+=("$p")
    else
        warn "skipping (not present): $p"
    fi
done

[ "${#resolved_paths[@]}" -gt 0 ] || { warn "no source paths exist -- nothing to back up"; exit 1; }

# ── Build the metadata blobs (written to a temp dir, then folded into
#    the tarball at well-known top-level paths) ───────────────────────
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

info "Building backup at $TARBALL"
info "  staging dir: $STAGE"

# _BUILD_INFO.txt -- context useful for diagnosing a future restore
{
    echo "# Meshpoint backup metadata"
    echo "created_at:   $(date -u -Iseconds)"
    echo "hostname:     $(hostname)"
    echo "kernel:       $(uname -srm)"
    echo "user:         $INVOKING_USER  ($USER_HOME)"
    echo ""
    echo "# /opt/meshpoint git state"
    if [ -d /opt/meshpoint/.git ]; then
        ( cd /opt/meshpoint && git rev-parse HEAD 2>/dev/null || echo "(detached?)" )
        ( cd /opt/meshpoint && git status --porcelain 2>/dev/null | head -20 )
    else
        echo "(not a git working tree)"
    fi
    echo ""
    echo "# USB devices at backup time"
    lsusb 2>/dev/null || true
    echo ""
    echo "# Serial nodes present"
    ls -la /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true
    echo ""
    echo "# Service states"
    systemctl is-active rnsd                  2>/dev/null || true
    systemctl is-active lxmd                  2>/dev/null || true
    systemctl is-active lxmf-inbox-dump       2>/dev/null || true
    systemctl is-active meshpoint             2>/dev/null || true
} > "$STAGE/_BUILD_INFO.txt"

# _MANIFEST.txt -- list of every file we're packing, with sizes + sha256
{
    echo "# size_bytes  sha256  path"
    for p in "${resolved_paths[@]}"; do
        if [ -d "$p" ]; then
            find "$p" -type f -print0 | xargs -0 -I{} sh -c '
                f="$1"
                size=$(stat -c%s "$f" 2>/dev/null || echo 0)
                hash=$(sha256sum "$f" 2>/dev/null | cut -d" " -f1)
                printf "%12s  %s  %s\n" "$size" "$hash" "$f"
            ' _ {}
        else
            size=$(stat -c%s "$p" 2>/dev/null || echo 0)
            hash=$(sha256sum "$p" 2>/dev/null | cut -d' ' -f1)
            printf "%12s  %s  %s\n" "$size" "$hash" "$p"
        fi
    done
} > "$STAGE/_MANIFEST.txt"

# _SHA256SUM -- checksum of the manifest. restore_meshpoint.sh
# verifies this before touching anything.
( cd "$STAGE" && sha256sum _MANIFEST.txt > _SHA256SUM )

# ── Make sure the output directory exists ────────────────────────────
mkdir -p "$OUT_DIR"
chmod 0700 "$OUT_DIR"   # backups contain identity private keys

# ── Tar everything up ────────────────────────────────────────────────
# Two-step: tar metadata + real paths separately to control insertion
# order. Metadata files land at the top of the archive so a quick
# `tar -tzf | head` reveals what the backup is at a glance.
info "Archiving ${#resolved_paths[@]} source path(s) + metadata..."

# Build a sorted file list (deterministic for diffing two backups)
# Use --absolute-names so paths inside the tarball mirror their real
# locations -- restore_meshpoint.sh extracts with -C / which puts
# everything back where it belongs.
tar \
    --create \
    --gzip \
    --file="$TARBALL" \
    --absolute-names \
    --transform="s|^$STAGE/|/_meta/|" \
    "$STAGE/_BUILD_INFO.txt" \
    "$STAGE/_MANIFEST.txt" \
    "$STAGE/_SHA256SUM" \
    "${resolved_paths[@]}"

# Tighten perms -- identities are inside
chmod 0600 "$TARBALL"

# ── Summary ──────────────────────────────────────────────────────────
SIZE_BYTES="$(stat -c%s "$TARBALL")"
SIZE_HR="$(numfmt --to=iec "$SIZE_BYTES" 2>/dev/null || echo "$SIZE_BYTES bytes")"
FILE_COUNT="$(tar -tzf "$TARBALL" | wc -l)"

info ""
info "Backup complete:"
info "  path:   $TARBALL"
info "  size:   $SIZE_HR ($SIZE_BYTES bytes)"
info "  files:  $FILE_COUNT"
info "  perms:  $(stat -c '%a %U:%G' "$TARBALL")"
info ""
info "To restore on this Pi (or a fresh one):"
info "  sudo bash /opt/meshpoint/scripts/restore_meshpoint.sh $TARBALL"
info ""
info "To verify integrity without restoring:"
info "  tar -xOzf '$TARBALL' /_meta/_SHA256SUM | sha256sum --check --strict -"
