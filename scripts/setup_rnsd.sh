#!/usr/bin/env bash
#
# setup_rnsd.sh -- install the Reticulum + LXMF messaging stack
# alongside Meshpoint on the same Raspberry Pi.
#
# This is the "B1" architecture from the rnsdusb branch design:
# rnsd and lxmd run as their own systemd services and own the USB
# RNode dongle for Reticulum messaging. Meshpoint continues running
# in parallel and captures Meshtastic on the concentrator. The two
# stacks don't share the radio -- they share the Pi.
#
# What this script does (all steps idempotent, safe to re-run):
#   1. Installs `rns` and `lxmf` Python packages via pip --user
#   2. Adds the invoking user to the `dialout` group so rnsd can open
#      the USB RNode without sudo
#   3. Drops example configs into ~/.reticulum/config and ~/.lxmd/config
#      (only if those files don't already exist -- never overwrites)
#   4. Installs rnsd.service and lxmd.service into /etc/systemd/system
#      with the invoking user's username and pip binary path
#      substituted in (so it works for any user, not just `mp`)
#   5. Disables Meshpoint's USB serial auto-detect in local.yaml so
#      Meshpoint doesn't fight rnsd over /dev/ttyUSB*
#   6. Enables and starts both services
#
# Usage:
#   bash /opt/meshpoint/scripts/setup_rnsd.sh
#
# After install, get your LXMF address with:
#   journalctl -u lxmd --no-pager | grep "ready to receive" | tail -1
#

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_DIR="$REPO_DIR/scripts/templates"

INVOKING_USER="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"

info()  { echo "[setup_rnsd] $*"; }
warn()  { echo "[setup_rnsd] WARN: $*" >&2; }
fail()  { echo "[setup_rnsd] ERROR: $*" >&2; exit 1; }

[ -d "$TEMPLATE_DIR" ] || fail "Templates not found at $TEMPLATE_DIR. Run from a Meshpoint checkout."

# ── 1. Install rns + lxmf via pip (user-level) ───────────────────────
# inotify_simple is for the lxmf_inbox_dump.py sidecar (Phase 2 #3) -- it
# watches lxmd's messagestore via kernel inotify so the dashboard inbox
# updates with sub-second latency instead of polling. Tiny pure-Python
# ctypes wrapper, no compile step.
#
# --break-system-packages: required on Debian 12 Bookworm / Pi OS 2023+
# where PEP 668 marks the system Python as "externally managed" and
# pip refuses to install without an explicit override. The flag is
# misnamed -- it does NOT touch apt-managed system packages; it only
# permits installing into the user's ~/.local/. We deliberately keep
# rns/lxmf out of a venv because lxmsendmsg / rnsd / lxmd are designed
# to live on the user's PATH at ~/.local/bin and the systemd units
# point straight at those paths.
PIP_FLAGS="--user --upgrade --break-system-packages"
info "Installing rns + lxmf + inotify_simple via pip ($PIP_FLAGS) for $INVOKING_USER..."
sudo -u "$INVOKING_USER" pip install $PIP_FLAGS rns lxmf inotify_simple

RNSD_BIN="$USER_HOME/.local/bin/rnsd"
LXMD_BIN="$USER_HOME/.local/bin/lxmd"

[ -x "$RNSD_BIN" ] || fail "rnsd not found at $RNSD_BIN after pip install"
[ -x "$LXMD_BIN" ] || fail "lxmd not found at $LXMD_BIN after pip install"

# ── 2. dialout group so rnsd can open the USB radio ──────────────────
if id -nG "$INVOKING_USER" | tr ' ' '\n' | grep -qx dialout; then
    info "$INVOKING_USER is already in dialout group"
else
    info "Adding $INVOKING_USER to dialout group (takes effect on next login)"
    sudo usermod -a -G dialout "$INVOKING_USER"
fi

# ── 3. Drop config files only if missing ─────────────────────────────
RNS_CONFIG_DIR="$USER_HOME/.reticulum"
LXM_CONFIG_DIR="$USER_HOME/.lxmd"

sudo -u "$INVOKING_USER" mkdir -p "$RNS_CONFIG_DIR" "$LXM_CONFIG_DIR"

if [ -f "$RNS_CONFIG_DIR/config" ]; then
    info "Reticulum config already exists at $RNS_CONFIG_DIR/config -- leaving alone"
else
    info "Installing Reticulum config from template"
    sudo -u "$INVOKING_USER" cp "$TEMPLATE_DIR/reticulum-config.example" "$RNS_CONFIG_DIR/config"
fi

if [ -f "$LXM_CONFIG_DIR/config" ]; then
    info "LXMF config already exists at $LXM_CONFIG_DIR/config -- leaving alone"
else
    info "Installing LXMF config from template"
    sudo -u "$INVOKING_USER" cp "$TEMPLATE_DIR/lxmd-config.example" "$LXM_CONFIG_DIR/config"
fi

# ── 4. Install systemd units with placeholders substituted ───────────
install_unit() {
    local template_name="$1"
    local exec_path="$2"
    local target="/etc/systemd/system/$template_name"

    info "Installing $target"
    sudo sed \
        -e "s|__USER__|$INVOKING_USER|g" \
        -e "s|__EXEC__|$exec_path|g" \
        "$TEMPLATE_DIR/$template_name" | sudo tee "$target" > /dev/null
    sudo chmod 644 "$target"
}

install_unit "rnsd.service" "$RNSD_BIN"
install_unit "lxmd.service" "$LXMD_BIN"

# Phase 2 #3: the inbox-dumper sidecar exec is a script in this repo,
# not a binary from pip. Same install_unit helper, different exec path.
DUMPER_SCRIPT="$REPO_DIR/scripts/lxmf_inbox_dump.py"
if [ -f "$DUMPER_SCRIPT" ]; then
    sudo chmod 755 "$DUMPER_SCRIPT"
    install_unit "lxmf-inbox-dump.service" "$DUMPER_SCRIPT"
else
    warn "lxmf_inbox_dump.py not found at $DUMPER_SCRIPT -- skipping inbox dumper"
fi

# Phase 2 #3: sudoers rule so the meshpoint dashboard user can invoke
# scripts/lxmf_send.py as the rnsd user (typically mp) for the send
# endpoint. The upstream lxmf pip package does NOT ship a standalone
# send CLI -- only the lxmd daemon -- so we wrap our own tiny sender
# script (which DOES import LXMF, but stays in the mp user's venv,
# preserving the decoupling rule we set in #1+#2).
#
# Render template -> tmpfile -> visudo -cf validate -> install.
# We validate BEFORE moving into place because a broken file in
# /etc/sudoers.d/ can lock the box out of sudo entirely.
SUDOERS_TEMPLATE="$TEMPLATE_DIR/meshpoint-lxmf.sudoers"
SEND_SCRIPT="$REPO_DIR/scripts/lxmf_send.py"
if [ -f "$SUDOERS_TEMPLATE" ] && [ -f "$SEND_SCRIPT" ]; then
    sudo chmod 755 "$SEND_SCRIPT"
    SUDOERS_TMP="$(mktemp)"
    sed -e "s|__USER__|$INVOKING_USER|g" \
        -e "s|__LXMSENDMSG__|$SEND_SCRIPT|g" \
        "$SUDOERS_TEMPLATE" > "$SUDOERS_TMP"
    if sudo visudo -cf "$SUDOERS_TMP" >/dev/null; then
        info "Installing /etc/sudoers.d/meshpoint-lxmf"
        sudo install -o root -g root -m 0440 \
            "$SUDOERS_TMP" /etc/sudoers.d/meshpoint-lxmf
    else
        warn "Rendered sudoers failed visudo -cf -- NOT installing (send endpoint will 403)"
    fi
    rm -f "$SUDOERS_TMP"
elif [ ! -f "$SEND_SCRIPT" ]; then
    warn "$SEND_SCRIPT missing -- skipping sudoers rule (send endpoint will 403)"
fi

# Phase 2 #3: meshpoint writes its sent-message log to /opt/meshpoint/data
# so the inbox endpoint can show both sent + received in thread view.
# Ensure the dir exists and meshpoint owns it.
MESHPOINT_DATA_DIR="/opt/meshpoint/data"
if id -u meshpoint >/dev/null 2>&1; then
    sudo mkdir -p "$MESHPOINT_DATA_DIR"
    sudo chown meshpoint:meshpoint "$MESHPOINT_DATA_DIR"
    sudo chmod 755 "$MESHPOINT_DATA_DIR"
fi

sudo systemctl daemon-reload

# ── 5. Disable Meshpoint's USB serial auto-detect ────────────────────
MESHPOINT_LOCAL="/opt/meshpoint/config/local.yaml"
if [ -f "$MESHPOINT_LOCAL" ]; then
    info "Ensuring Meshpoint does NOT auto-detect /dev/ttyUSB* (rnsd owns it)"
    sudo python3 - "$MESHPOINT_LOCAL" <<'PYEOF'
import sys, yaml
from pathlib import Path

p = Path(sys.argv[1])
cfg = yaml.safe_load(p.read_text()) or {}
cap = cfg.setdefault("capture", {})

changed = False
for sub in ("rnode_usb", "meshcore_usb"):
    block = cap.setdefault(sub, {})
    if block.get("auto_detect") is not False:
        block["auto_detect"] = False
        changed = True
    if block.get("serial_port") not in (None, ""):
        block["serial_port"] = None
        changed = True

if changed:
    p.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
    print("local.yaml updated")
else:
    print("local.yaml already disables USB serial auto-detect")
PYEOF
    info "Restarting meshpoint to release any held USB ports"
    sudo systemctl restart meshpoint 2>/dev/null || warn "meshpoint not running -- skipping restart"
else
    warn "Meshpoint local.yaml not found at $MESHPOINT_LOCAL; skipping auto-detect disable"
fi

# ── 5a. Grant meshpoint user access to systemd journals ─────────────
# Meshpoint's /api/reticulum/* shim greps `journalctl -u rnsd` /
# `journalctl -u lxmd` to extract identity + announce data. By default
# only adm and systemd-journal group members can read other users'
# unit journals.
if id -u meshpoint >/dev/null 2>&1; then
    if id -nG meshpoint | tr ' ' '\n' | grep -qx systemd-journal; then
        info "meshpoint already in systemd-journal group"
    else
        info "Adding meshpoint to systemd-journal group (for /api/reticulum/* journal reads)"
        sudo usermod -aG systemd-journal meshpoint
        info "Meshpoint will need a restart to inherit the new group"
    fi
else
    warn "meshpoint user not found -- skipping journal group grant"
fi

# ── 5b. Make rnsd/lxmd artifacts readable by the meshpoint user ──────
# Meshpoint's /api/reticulum/* shim needs to read the lxmd config and
# execute rnstatus from the rnsd user's $HOME. On stock Raspbian /home
# is mode 755 but some hardenings make it 700 and Meshpoint then sees
# PermissionError. Make just the specific paths world-traversable /
# readable -- not the whole home dir.
info "Granting meshpoint read access to rnsd/lxmd artifacts"
for path in \
    "$USER_HOME" \
    "$USER_HOME/.lxmd" \
    "$USER_HOME/.lxmd/config" \
    "$USER_HOME/.local" \
    "$USER_HOME/.local/bin" \
    "$USER_HOME/.local/bin/rnstatus"; do
    if [ -e "$path" ]; then
        # +rx for directories (traversal), +r for files
        sudo chmod o+rX "$path" 2>/dev/null || true
    fi
done

# ── 6. Enable + start the services ───────────────────────────────────
info "Enabling and starting rnsd.service"
sudo systemctl enable --now rnsd.service

info "Enabling and starting lxmd.service"
sudo systemctl enable --now lxmd.service

# Phase 2 #3: the inbox dumper sidecar. Only enable if the unit was
# actually installed above (the script may have been skipped on a
# stripped-down repo checkout).
if [ -f /etc/systemd/system/lxmf-inbox-dump.service ]; then
    info "Enabling and starting lxmf-inbox-dump.service"
    sudo systemctl enable --now lxmf-inbox-dump.service
fi

sleep 5

# ── 7. Verify ────────────────────────────────────────────────────────
info "Service status:"
sudo systemctl is-active rnsd.service lxmd.service || true

info ""
info "Reticulum interfaces (rnstatus):"
sudo -u "$INVOKING_USER" "$USER_HOME/.local/bin/rnstatus" 2>&1 | head -30 || true

info ""
info "Your LXMF address (paste this into peers' MeshChat / Sideband etc.):"
sleep 2
journalctl -u lxmd --no-pager | grep "ready to receive" | tail -1 \
    || warn "LXMF address not in journal yet -- check with: journalctl -u lxmd | grep 'ready to receive'"

info ""
info "Done. See docs/RNS-LXMF-SETUP.md for next steps and troubleshooting."
