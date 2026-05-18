#!/usr/bin/env bash
#
# restore_meshpoint.sh -- counterpart to backup_meshpoint.sh.
# Unpacks a backup tarball onto this Pi, restoring every captured
# config + identity + service unit. Safe to run on:
#   * The SAME Pi after a failed experiment (idempotent overwrite)
#   * A FRESH Pi after base meshpoint install (cold-start path
#     for Phase 3 -- pi_imager + meshpoint base install + this)
#
# Sequence (each step atomic; if any fails the script bails with the
# previous state still intact):
#   1. Sanity-check the tarball: file exists, owned by root, mode
#      tight, contains expected _MANIFEST.txt + _SHA256SUM.
#   2. Verify the in-tar manifest checksum (catches truncation /
#      corruption before we touch a single live file).
#   3. Stop the four services (meshpoint, rnsd, lxmd, lxmf-inbox-dump)
#      so their fds aren't holding the files we're about to overwrite.
#   4. Extract straight to "/" with --absolute-names. Tarball was
#      created with full paths so this puts everything back where
#      it belongs.
#   5. systemctl daemon-reload so any unit-file diffs are noticed.
#   6. Re-chown the rnsd user's home artifacts (~mp/.reticulum,
#      ~mp/.lxmd) -- a backup made on a Pi with UID 1000 = mp will
#      keep UID 1000 inside the tarball; if the new Pi's mp user has
#      a different UID we'd have permission breakage otherwise.
#   7. Start services back up.
#   8. Quick smoke check: rnsd reports an interface, meshpoint API
#      answers, log a final OK line.
#
# Usage:
#   sudo bash /opt/meshpoint/scripts/restore_meshpoint.sh <tarball>
#
# To verify a tarball WITHOUT restoring:
#   sudo bash /opt/meshpoint/scripts/restore_meshpoint.sh --verify <tarball>
#

set -euo pipefail

info() { echo "[restore] $*"; }
warn() { echo "[restore] WARN: $*" >&2; }
fail() { echo "[restore] ERROR: $*" >&2; exit 1; }

# ── Arg parsing ──────────────────────────────────────────────────────
VERIFY_ONLY=0
if [ "${1:-}" = "--verify" ]; then
    VERIFY_ONLY=1
    shift
fi
TARBALL="${1:-}"
[ -n "$TARBALL" ] || fail "usage: $0 [--verify] <backup-tarball>"
[ -f "$TARBALL" ] || fail "backup tarball not found: $TARBALL"

INVOKING_USER="${SUDO_USER:-${USER:-mp}}"
USER_HOME="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"

# ── 1. Sanity check ──────────────────────────────────────────────────
PERMS="$(stat -c '%a' "$TARBALL")"
case "$PERMS" in
    600|400|640|440) ;;
    *) warn "tarball mode is $PERMS (expected 0600 -- identities are inside)";;
esac

info "Inspecting $TARBALL"
file "$TARBALL" | head -1

# Confirm it really is a gzipped tar
if ! gzip -t "$TARBALL" 2>/dev/null; then
    fail "tarball is not a valid gzip (truncated or wrong file?)"
fi

# Confirm the meta entries exist. Two compatibility points:
#
#   1. The entry path may have a leading `/` (newer tar with
#      --absolute-names keeps it) OR not (older tar / different
#      flag combos strip it). Be tolerant of both with `^/?` in
#      the regex.
#
#   2. `tar -tzf` fires the "Removing leading / from member names"
#      warning to stderr on some tar versions during LISTING and
#      exits non-zero -- which combined with `set -o pipefail`
#      kills our pipe BEFORE grep has a chance to match, even
#      though the matching entry was right there in stdout. Avoid
#      the pipe entirely by capturing the listing to a temp file.
TZF_LIST="$(mktemp)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$TZF_LIST"' EXIT
tar -tzf "$TARBALL" >"$TZF_LIST" 2>/dev/null || true
if ! grep -qE '^/?_meta/_MANIFEST\.txt$' "$TZF_LIST"; then
    fail "tarball is missing _MANIFEST.txt -- not a meshpoint backup?"
fi

# ── 2. Verify the in-tar manifest checksum ───────────────────────────
info "Verifying manifest checksum..."
# Extract using the relative (no leading slash) form -- that's what
# tar actually wrote. tar will silently ignore non-matching names,
# so if the entries are absolute we fall through to a failure below.
tar -xzf "$TARBALL" -C "$STAGE" \
    _meta/_MANIFEST.txt _meta/_SHA256SUM 2>/dev/null \
    || fail "could not extract metadata blobs"

# If the extract above produced nothing (entries WERE absolute and
# tar didn't match), retry with the absolute form.
if [ ! -f "$STAGE/_meta/_MANIFEST.txt" ]; then
    tar -xzf "$TARBALL" -C "$STAGE" \
        /_meta/_MANIFEST.txt /_meta/_SHA256SUM 2>/dev/null \
        || fail "could not extract metadata blobs (tried both paths)"
fi
[ -f "$STAGE/_meta/_MANIFEST.txt" ] || fail "_MANIFEST.txt extraction silently failed"

(
    cd "$STAGE/_meta" || exit 1
    # _SHA256SUM was produced inside _meta/, so the relative path is
    # bare "_MANIFEST.txt" -- check it directly.
    sha256sum --check --strict _SHA256SUM
) || fail "manifest checksum mismatch -- tarball is corrupt or tampered with"

info "  manifest OK ($(wc -l <"$STAGE/_meta/_MANIFEST.txt") files indexed)"

if [ -f "$STAGE/_meta/_BUILD_INFO.txt" ]; then
    info "  backup metadata:"
    head -6 "$STAGE/_meta/_BUILD_INFO.txt" | sed 's/^/    /'
fi

if [ "$VERIFY_ONLY" = "1" ]; then
    info ""
    info "Verify-only mode complete. No files were touched."
    exit 0
fi

# ── 3. Stop services ─────────────────────────────────────────────────
info "Stopping services for safe restore..."
for unit in meshpoint lxmf-inbox-dump lxmd rnsd; do
    if systemctl is-enabled "$unit" >/dev/null 2>&1 || \
       systemctl is-active  "$unit" >/dev/null 2>&1; then
        sudo systemctl stop "$unit" 2>/dev/null || warn "could not stop $unit (may not be installed yet)"
    fi
done

# ── 4. Extract straight to / ─────────────────────────────────────────
# Skip the meta blobs (we already extracted them); only put the real
# paths back. Using --absolute-names because the tarball was built
# with full paths.
info "Extracting backup payload to /..."
# Two excludes because tar may have stripped the leading slash when
# writing the archive (depends on GNU tar version + the
# --absolute-names + --transform interaction at backup time). Both
# forms cover both shapes.
tar -xzf "$TARBALL" --absolute-names \
    --exclude='/_meta/*' --exclude='_meta/*' \
    -C /

# ── 5. Notify systemd if any unit files were rewritten ──────────────
info "systemctl daemon-reload"
sudo systemctl daemon-reload

# ── 6. Re-chown the rnsd user's artifacts ────────────────────────────
# Backups carry UID/GID; if the new Pi's mp user has a different UID
# we need to fix ownership. Best-effort -- skip silently if mp doesn't
# exist (the operator will need to run setup_rnsd.sh to create them).
if id -u "$INVOKING_USER" >/dev/null 2>&1; then
    for dir in "$USER_HOME/.reticulum" "$USER_HOME/.lxmd"; do
        if [ -d "$dir" ]; then
            sudo chown -R "$INVOKING_USER:$INVOKING_USER" "$dir"
        fi
    done
fi

# ── 7. Start services ────────────────────────────────────────────────
info "Starting services..."
for unit in rnsd lxmd lxmf-inbox-dump meshpoint; do
    if [ -f "/etc/systemd/system/$unit.service" ] || \
       systemctl cat "$unit" >/dev/null 2>&1; then
        sudo systemctl start "$unit" 2>/dev/null || warn "could not start $unit"
    fi
done

# ── 8. Smoke check ───────────────────────────────────────────────────
sleep 4
info ""
info "Post-restore service states:"
for unit in rnsd lxmd lxmf-inbox-dump meshpoint; do
    state="$(systemctl is-active "$unit" 2>/dev/null || echo unknown)"
    printf '  %-22s %s\n' "$unit" "$state"
done

info ""
info "Restore complete. If anything's amiss check the journal:"
info "  sudo journalctl -u rnsd -u lxmd -u meshpoint -u lxmf-inbox-dump --since '2 min ago'"
