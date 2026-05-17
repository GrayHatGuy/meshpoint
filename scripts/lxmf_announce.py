#!/usr/bin/env python3
"""Trigger a single LXMF delivery-destination announce.

Runs as the user that owns the lxmd identity (typically `mp`),
invoked by Meshpoint's POST /api/reticulum/announce endpoint via a
narrow sudoers rule (extends the existing meshpoint-lxmf.sudoers
that already covers lxmf_send.py).

Why this script exists: lxmd's [lxmf] section in ~/.lxmd/config
supports `announce_at_start = yes` but has no periodic
announce_interval -- meaning after the initial startup announce,
peers can lose path to us as their caches age. Operators need a
way to:
  * Re-announce on demand (network changed, propagation hop went
    down, etc.) -- the "Send Now" button in the dashboard.
  * Schedule periodic announces (every N minutes) without
    bouncing lxmd to pick up a new config -- the sidecar fires
    this script on a saved cadence.

Both flows shell out to the same script for one reason: the LXMF
announce path needs to use lxmd's exact identity so peers see the
same source hash they get on startup. Building this in the
Meshpoint venv would either duplicate identity-loading code or
break the decoupling rule. A small sudo-bridge keeps things tidy.

Usage:
    lxmf_announce.py [--app-data TEXT] [--timeout SECS]

Exit codes:
    0 -- announce dispatched to RNS, packet sent on the radio
    1 -- preflight failed (RNS/LXMF not importable, identity missing)
    2 -- announce call raised inside RNS/LXMF
    3 -- timed out waiting for the announce to reach the air

On success, prints the destination hash (hex) for caller logging.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import RNS  # type: ignore
    import LXMF  # type: ignore
except ImportError as exc:
    sys.stderr.write(
        f"ERROR: RNS/LXMF not importable in this environment: {exc}\n"
        "Run: pip install --user --upgrade --break-system-packages rns lxmf\n"
    )
    sys.exit(1)


def _eprint(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _read_lxmd_display_name(default: str = "Meshpoint") -> str:
    """Best-effort parse of [lxmf] display_name from ~/.lxmd/config.

    We need the display_name so the announce's app_data field
    matches what peers see when lxmd does its startup announce.
    Without this, the operator's chosen MeshChat-equivalent name
    would silently revert to "" / hash-only on every re-announce.
    """
    cfg_path = os.path.expanduser("~/.lxmd/config")
    if not os.path.isfile(cfg_path):
        return default
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return default
    in_lxmf = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_lxmf = (line.strip("[]").strip() == "lxmf")
            continue
        if in_lxmf and line.startswith("display_name"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip() or default
    return default


def main() -> int:
    ap = argparse.ArgumentParser(description="Send one LXMF announce")
    ap.add_argument(
        "--app-data", default=None,
        help="Override display_name in app_data. Defaults to "
             "[lxmf] display_name from ~/.lxmd/config.",
    )
    ap.add_argument(
        "--timeout", type=float, default=10.0,
        help="Seconds to allow RNS to dispatch the announce "
             "(default: 10)",
    )
    args = ap.parse_args()

    # ── Load lxmd's identity ─────────────────────────────────────
    identity_path = os.path.expanduser("~/.lxmd/identity")
    if not os.path.isfile(identity_path):
        _eprint(f"ERROR: lxmd identity not found at {identity_path}")
        return 1
    try:
        identity = RNS.Identity.from_file(identity_path)
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: could not load lxmd identity: {exc}")
        return 1

    # ── Attach to running rnsd via shared-instance socket ────────
    try:
        RNS.Reticulum(loglevel=0)
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: could not connect to RNS: {exc}")
        return 1

    # ── Build the LXMF delivery destination ──────────────────────
    # IN/SINGLE with aspects "lxmf"/"delivery" is the standard LXMF
    # delivery destination -- the same one lxmd opens on startup.
    # Peers see this hash in /api/reticulum/identity.
    try:
        destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE,
            "lxmf", "delivery",
        )
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: could not build delivery destination: {exc}")
        return 2

    # ── Compose app_data so peers see our display name ──────────
    name = args.app_data or _read_lxmd_display_name()
    try:
        app_data = name.encode("utf-8")
    except Exception:  # noqa: BLE001
        app_data = name.encode("utf-8", errors="replace")

    # ── Fire the announce ────────────────────────────────────────
    try:
        destination.announce(app_data=app_data)
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: announce call raised: {exc}")
        return 2

    # Give RNS a moment to actually serialize and ship the packet
    # to the RNode interface before we exit (otherwise the process
    # tears down the shared-instance connection mid-send). Short
    # poll loop with a small initial settle so we don't pin the
    # CPU if RNS finishes immediately.
    deadline = time.time() + max(0.5, args.timeout)
    time.sleep(0.5)
    while time.time() < deadline:
        time.sleep(0.25)
        # No clean "has the announce gone out?" hook in RNS; we
        # just wait a reasonable settle window. Total wait is
        # bounded by --timeout above.
        break

    print(destination.hash.hex())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
