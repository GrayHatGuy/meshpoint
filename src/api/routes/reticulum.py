"""Read-only API endpoints exposing the standalone rnsd + lxmd state.

This router is part of the "B1" architecture (see docs/RNS-LXMF-SETUP.md):
rnsd and lxmd run as separate systemd services with their own identities
and configs in the invoking user's home directory. Meshpoint deliberately
does NOT import RNS or LXMF, so the dashboard stays decoupled from the
messaging stack -- if the RNS install moves to a different venv, or the
services run under a different user, only the path constants here change.

All endpoints are read-only. Writes (send a message, trigger an announce,
edit identity display name) are out of scope for Phase 2 #1 + #2 and
will be added separately if the operator opts into the in-dashboard
messaging UI (Phase 2 #3).

Data sources:
    /api/reticulum/identity   - parsed from lxmd's journalctl output
                                ("LXMF Router ready to receive on <hash>")
                                plus display_name read from ~mp/.lxmd/config
    /api/reticulum/status     - systemctl is-active for rnsd + lxmd, plus
                                a single `rnstatus` invocation per request
                                for interface up/down + airtime
    /api/reticulum/peers      - parsed from rnsd's journalctl output
                                ("Valid announce for <hash> N hops away...")
                                de-duplicated to the most recent sighting
                                per destination hash
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reticulum", tags=["reticulum"])

# ── Where the rnsd/lxmd state lives ──────────────────────────────────
# These paths assume the standard B1 install (see setup_rnsd.sh): rnsd
# and lxmd run under a regular Linux user with configs in $HOME. The
# meshpoint service runs under a different user but can read these
# files because they're world-readable by default.
#
# If you change the deployment so rnsd/lxmd run as a different user,
# point _RNSD_USER at that user's name.
_RNSD_USER = os.environ.get("MESHPOINT_RNSD_USER", "mp")
_RNSD_HOME = Path(f"/home/{_RNSD_USER}")
_LXMD_CONFIG = _RNSD_HOME / ".lxmd" / "config"
_KNOWN_DESTINATIONS = _RNSD_HOME / ".reticulum" / "storage" / "known_destinations"

# Cap how much journal history we read per request.
# Reticulum announces typically fire every few hours per destination,
# so 1 hour was too tight -- a quiet network would show zero peers
# even though they were just heard. 24 hours gives a one-day rolling
# view of the LoRa neighborhood, which is what operators expect from
# a "recently heard" peer list. The lxmd ready-to-receive line is
# emitted only at process start, so we keep a wider window for that.
_ANNOUNCE_JOURNAL_SINCE = "24 hours ago"
_IDENTITY_JOURNAL_SINCE = "30 days ago"

# Subprocess timeouts. The whole point of read-only endpoints is they
# return fast; a stalled journalctl or rnstatus should never block the
# UI more than a few seconds.
_SUBPROC_TIMEOUT_SEC = 3.0


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/identity")
async def get_identity() -> dict:
    """Return the local LXMF address + display name + service liveness.

    The address is whatever lxmd printed on its most recent
    "ready to receive" line -- this is the LXMF delivery destination
    hash, NOT the RNS transport identity. Peers send to THIS address.
    """
    return {
        "address": _extract_lxmf_address(),
        "display_name": _extract_lxmd_display_name(),
        "rnsd_running": _is_service_active("rnsd.service"),
        "lxmd_running": _is_service_active("lxmd.service"),
    }


@router.get("/status")
async def get_status() -> dict:
    """High-level rnsd/lxmd liveness and interface summary.

    Returns service state plus a single rnstatus snapshot. The
    rnstatus parse is best-effort -- if the binary is missing or the
    output format changes, the interfaces array is empty rather than
    raising 500.
    """
    return {
        "rnsd_running": _is_service_active("rnsd.service"),
        "lxmd_running": _is_service_active("lxmd.service"),
        "interfaces": _parse_rnstatus(),
    }


@router.get("/peers")
async def get_peers(limit: int = 50) -> dict:
    """Recently-heard Reticulum destinations from rnsd announces.

    Each peer entry is the most-recent announce seen for that hash
    within the last hour. Older entries are pruned naturally because
    we only scan a bounded journal window.
    """
    peers = _parse_recent_announces(limit=limit)
    return {"peers": peers, "count": len(peers)}


# ── Internal helpers ─────────────────────────────────────────────────


def _is_service_active(unit_name: str) -> bool:
    """systemctl is-active wrapper; returns False on any error."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit_name],
            capture_output=True, text=True,
            timeout=_SUBPROC_TIMEOUT_SEC,
        )
        return result.stdout.strip() == "active"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _journalctl(unit_name: str, since: str) -> str:
    """journalctl --no-pager wrapper. Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit_name, "--no-pager", "--since", since],
            capture_output=True, text=True,
            timeout=_SUBPROC_TIMEOUT_SEC,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("journalctl %s failed: %s", unit_name, exc)
        return ""


def _extract_lxmf_address() -> Optional[str]:
    """Pull the most recent 'LXMF Router ready to receive on <hash>' from lxmd log."""
    log = _journalctl("lxmd.service", _IDENTITY_JOURNAL_SINCE)
    matches = re.findall(
        r"LXMF Router ready to receive on <([0-9a-f]{16,})>",
        log,
    )
    return matches[-1] if matches else None


def _extract_lxmd_display_name() -> str:
    """Parse display_name out of the lxmd config. Falls back to 'Meshpoint'.

    Wrapped in a broad try/except because the meshpoint service runs
    as a different user from the one that owns ~/.lxmd/config; any
    OS-level access error (including PermissionError from `.exists()`
    on a 700-mode home directory) should degrade silently to the
    default display_name rather than 500ing the endpoint.
    """
    try:
        if not _LXMD_CONFIG.exists():
            return "Meshpoint"
        text = _LXMD_CONFIG.read_text()
    except OSError:
        return "Meshpoint"

    # Find display_name = ... inside the [lxmf] section. Quick-and-dirty
    # because we don't want a configobj dep here just for one field.
    in_lxmf = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_lxmf = (line.strip("[]").strip() == "lxmf")
            continue
        if in_lxmf and line.startswith("display_name"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return "Meshpoint"


def _parse_rnstatus() -> list[dict]:
    """Best-effort parse of `rnstatus` text output into interface dicts."""
    # rnstatus lives in the same bin dir as rnsd; try common locations.
    # Each candidate check is wrapped in a try/except since `.is_file()`
    # on a path inside another user's 700-mode home will raise.
    candidates = [
        _RNSD_HOME / ".local" / "bin" / "rnstatus",
        Path("/usr/local/bin/rnstatus"),
        Path("/usr/bin/rnstatus"),
    ]
    binary = None
    for p in candidates:
        try:
            if p.is_file() and os.access(p, os.X_OK):
                binary = p
                break
        except OSError:
            continue
    if binary is None:
        return []

    try:
        result = subprocess.run(
            [str(binary)],
            capture_output=True, text=True,
            timeout=_SUBPROC_TIMEOUT_SEC,
        )
        text = result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("rnstatus invocation failed: %s", exc)
        return []

    # rnstatus output sections start with " InterfaceType[Name]"
    # followed by indented "    Field    : value" lines until the next
    # section or end-of-output.
    interfaces: list[dict] = []
    current: Optional[dict] = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith(" ") and "[" in raw and "]" in raw and not raw.startswith("    "):
            if current is not None:
                interfaces.append(current)
            header = raw.strip()
            current = {"interface": header, "fields": {}}
            continue
        if current is not None and raw.startswith("    "):
            field_line = raw.strip()
            if ":" in field_line:
                k, v = field_line.split(":", 1)
                current["fields"][k.strip().lower().replace(" ", "_")] = v.strip()
    if current is not None:
        interfaces.append(current)

    return interfaces


_ANNOUNCE_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"\[(?P<level>\w+)\]\s+"
    r"Valid announce for <(?P<hash>[0-9a-f]+)>\s+"
    r"(?P<hops>\d+)\s+hops? away"
    r"(?:,\s+received(?:\s+via\s+<(?P<via>[0-9a-f]+)>)?\s+on\s+"
    r"(?P<interface>\S+(?:\s\S+)*?))?"
    r"(?:\s+\[RSSI\s+(?P<rssi>-?\d+)dBm,\s+SNR\s+(?P<snr>-?[\d.]+)dB\])?",
)


def _parse_recent_announces(limit: int = 50) -> list[dict]:
    """Pull recent 'Valid announce for <hash>' lines from rnsd journal.

    Returns up to ``limit`` peers, most-recent-first, de-duplicated
    per destination hash. Hops, last_heard, RSSI, SNR, and via-relay
    fields are surfaced when present in the log.

    Self-announces (0 hops via LocalInterface) are filtered out -- those
    are this node's own outbound LXMF/RNS announces echoing through the
    local rnsd loop, NOT actual peers heard over the radio. They'd
    otherwise show up as a confusing duplicate of "(this Meshpoint)"
    in the destinations card.
    """
    log = _journalctl("rnsd.service", _ANNOUNCE_JOURNAL_SINCE)
    if not log:
        return []

    # OrderedDict keyed by hash so we keep the most recent line per peer
    # by iterating chronologically and overwriting earlier entries.
    seen: OrderedDict[str, dict] = OrderedDict()

    for line in log.splitlines():
        m = _ANNOUNCE_RE.search(line)
        if not m:
            continue
        interface = (m.group("interface") or "").strip()
        hops = int(m.group("hops"))
        # Skip our own local-loopback announces.
        if hops == 0 and interface.startswith("LocalInterface"):
            continue
        entry = {
            "hash": m.group("hash"),
            "hops": hops,
            "via": m.group("via"),
            "interface": interface,
            "last_heard": _journal_ts_to_iso(m.group("ts")),
            "rssi": float(m.group("rssi")) if m.group("rssi") else None,
            "snr": float(m.group("snr")) if m.group("snr") else None,
        }
        seen[entry["hash"]] = entry  # overwrites older entry for same hash

    # Most-recent first; trim to limit.
    peers = list(seen.values())
    peers.sort(key=lambda p: p["last_heard"] or "", reverse=True)
    return peers[:limit]


def _journal_ts_to_iso(ts: str) -> Optional[str]:
    """Convert '2026-05-16 22:18:11' to an ISO 8601 string."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None
