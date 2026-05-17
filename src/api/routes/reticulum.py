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

import json
import logging
import os
import re
import subprocess
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

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

# ── Phase 2 #3: inbox + send paths ───────────────────────────────────
# inbox.json is written by the lxmf_inbox_dump.py sidecar (runs as the
# _RNSD_USER, inotify-driven). We only READ it here -- never write.
_INBOX_JSON = _RNSD_HOME / ".lxmd" / "inbox.json"
# Phase 1 #2: lxmf_peers.json is written by the SAME sidecar on a 60s
# periodic enrichment pass. Maps every known hash -> {display_name,
# class, is_lxmf, app_data_hex}. Used to substitute friendly names
# into /api/reticulum/peers and /api/reticulum/inbox responses.
_PEERS_JSON = _RNSD_HOME / ".lxmd" / "lxmf_peers.json"

# Phase 1 #2b: operator-edited contact overrides. Highest priority in
# the display-name chain (operator > classifier > placeholder). Lives
# under data/ rather than .lxmd/ because meshpoint owns it and a
# `git pull` of /opt/meshpoint must NEVER overwrite it.
#
# Schema:
#   {
#     "contacts": {
#       "<hex_hash>": {"nickname": "Alice", "notes": "neighbor 2 blocks south"},
#       ...
#     },
#     "updated_at": "<iso8601 utc>"
#   }
_CONTACTS_JSON = Path("data/lxmf_contacts.json")
# The upstream lxmf pip package ships only the lxmd daemon, no send
# CLI. We bundle our own tiny sender at scripts/lxmf_send.py and
# invoke it via `sudo -u <rnsd_user>` (the sudoers rule installed by
# setup_rnsd.sh narrows this to exactly this script path).
_LXMF_SEND_SCRIPT = Path("/opt/meshpoint/scripts/lxmf_send.py")
# Append-only log of messages WE sent, so the inbox endpoint can show
# both directions in a thread view. lxmd's messagestore is inbox-only --
# lxmsendmsg doesn't leave a local trace of what we sent.
_SENT_LOG = Path("data/lxmf_sent.jsonl")
# A send that takes longer than this is almost certainly a routing
# failure rather than a slow network -- 30s is well past the worst
# expected ack-window for a 3-hop LoRa path.
_SEND_TIMEOUT_SEC = 30.0

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


@router.get("/peer_map")
async def get_peer_map() -> dict:
    """Return the FULL peer enrichment map (Phase 1 #1).

    Unlike /peers (which is bounded to recently-heard announces),
    this returns every hash the sidecar's classifier has ever
    processed -- the same content as ~/.lxmd/lxmf_peers.json's
    `peers` dict.

    Used by the Dashboard Packets renderer to substitute display
    names + class badges for Reticulum packet source/dest hashes
    retroactively (the address book is a presentation layer; old
    packets render new names as soon as the classifier sees them).
    """
    return {
        "peers": _read_peers_enrichment(),
        "generated_at": _peers_generated_at(),
    }


def _peers_generated_at() -> Optional[str]:
    """Expose the sidecar's last peers.json write time for the UI."""
    try:
        with _PEERS_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        gen = payload.get("generated_at")
        return gen if isinstance(gen, str) else None
    except (OSError, json.JSONDecodeError):
        return None


@router.get("/peers")
async def get_peers(limit: int = 50) -> dict:
    """Recently-heard Reticulum destinations from rnsd announces.

    Each peer entry is the most-recent announce seen for that hash
    within the last hour. Older entries are pruned naturally because
    we only scan a bounded journal window.

    Enriched with display_name + class + is_lxmf from the sidecar's
    lxmf_peers.json when available. Peers missing from that JSON
    (e.g., heard for the first time in the last few seconds, before
    the sidecar's 60s enrich tick) get null fields rather than
    being omitted -- the UI can still show the hash.
    """
    peers = _parse_recent_announces(limit=limit)
    enrich = _read_peers_enrichment()
    contacts = read_contacts()
    for p in peers:
        h = p.get("hash") or ""
        meta = enrich.get(h, {})
        name, source = resolve_display_name(h, meta, contacts)
        p["display_name"]        = name
        p["display_name_source"] = source           # operator|classifier|none
        p["class"]               = meta.get("class") or "unknown"
        p["is_lxmf"]             = bool(meta.get("is_lxmf"))
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


# NB: interface names can contain spaces AND brackets, e.g.
# "RNodeInterface[Meshpoint RNode USB]". The lazy `.+?` plus the
# `\s*$` end-anchor force the engine to expand interface just enough
# to let the optional RSSI/SNR tail (or end-of-line) match -- without
# the anchor, lazy stops at the first space and the RSSI group is
# silently skipped, producing rssi/snr = None on perfectly good lines.
_ANNOUNCE_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"\[(?P<level>\w+)\]\s+"
    r"Valid announce for <(?P<hash>[0-9a-f]+)>\s+"
    r"(?P<hops>\d+)\s+hops? away"
    r"(?:,\s+received(?:\s+via\s+<(?P<via>[0-9a-f]+)>)?\s+on\s+"
    r"(?P<interface>.+?)"
    r"(?:\s+\[RSSI\s+(?P<rssi>-?\d+)dBm,\s+SNR\s+(?P<snr>-?[\d.]+)dB\])?"
    r")?"
    r"\s*$",
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


# ── Phase 2 #3: send + inbox ─────────────────────────────────────────


_HASH_RE = re.compile(r"^[0-9a-f]{32}$")


class SendMessageBody(BaseModel):
    """Request body for POST /api/reticulum/send.

    Fields mirror the lxmsendmsg CLI: destination, optional title,
    message body. We deliberately don't expose lxmsendmsg's --propagate
    or --propagate-via flags yet -- defaults work for direct delivery
    and adding routing knobs without a UX story for them just creates
    foot-guns.
    """
    destination_hash: str = Field(..., description="32-char lowercase hex")
    content: str = Field(..., min_length=1, max_length=4000)
    title: Optional[str] = Field(default=None, max_length=200)

    @field_validator("destination_hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HASH_RE.match(v):
            raise ValueError(
                "destination_hash must be 32 lowercase hex characters"
            )
        return v


@router.post("/send")
async def send_message(body: SendMessageBody) -> dict:
    """Send an LXMF message via lxmsendmsg.

    We shell out to the same CLI MeshChat / Sideband users would type
    interactively, via a tightly-scoped sudoers rule (see
    scripts/templates/meshpoint-lxmf.sudoers). This keeps Meshpoint's
    venv LXMF-free, same architectural rule as #1+#2.

    Returns 200 with `{"sent": true, ...}` on success. Failures bubble
    up as HTTP 4xx/5xx with the underlying error message verbatim --
    operators will need the lxmsendmsg stderr to diagnose routing
    issues (unreachable destination, no path, etc.).
    """
    # Positional args to lxmf_send.py: <destination_hash> <content>
    # plus optional --title. We pass the script path explicitly (not
    # via PATH) because the sudoers rule pins it -- any other path
    # would 403 with "command not allowed by sudoers."
    cmd = [
        "sudo", "-n", "-u", _RNSD_USER, str(_LXMF_SEND_SCRIPT),
        body.destination_hash, body.content,
    ]
    if body.title:
        cmd.extend(["--title", body.title])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_SEND_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"lxmsendmsg timed out after {_SEND_TIMEOUT_SEC}s",
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="sudo not available on this host",
        )

    if result.returncode != 0:
        # Common causes: sudoers rule missing/broken (rc=1, stderr
        # "a password is required"); lxmf_send.py couldn't reach
        # destination (rc=2); send timed out waiting for state=SENT
        # (rc=3); unexpected exception inside the sender (rc=4).
        # Pass through the actual error so the frontend can show
        # something diagnostic.
        stderr = (result.stderr or result.stdout or "").strip()
        logger.warning(
            "lxmf_send.py failed (rc=%d): %s", result.returncode, stderr,
        )
        raise HTTPException(
            status_code=502,
            detail=f"send failed: {stderr[:500]}" or "send failed",
        )

    sent_at = datetime.now(tz=timezone.utc)
    record = {
        "destination_hash": body.destination_hash,
        "title":            body.title or "",
        "content":          body.content,
        "timestamp":        sent_at.timestamp(),
        "sent_iso":         sent_at.isoformat(),
    }
    _append_sent_log(record)

    return {
        "sent":             True,
        "destination_hash": body.destination_hash,
        "sent_iso":         record["sent_iso"],
    }


def _append_sent_log(record: dict) -> None:
    """Append one JSON line to data/lxmf_sent.jsonl.

    Append-only because (a) it's the simplest format that's also
    crash-safe (no partial-rewrite risk like a single JSON array),
    (b) the inbox endpoint reads the file fully on every request so
    the read cost is the same shape, and (c) the file is bounded by
    operator behavior, not network traffic -- a few hundred messages
    over the lifetime of a deployment isn't a perf concern.

    setup_rnsd.sh provisions data/ with meshpoint:meshpoint
    ownership; on a fresh dev box where it doesn't exist, we
    best-effort mkdir and log on failure rather than raising (the
    send itself already succeeded -- losing the local log row is
    annoying but not a 500-worthy error).
    """
    try:
        _SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _SENT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Could not append to %s: %s", _SENT_LOG, exc)


@router.get("/inbox")
async def get_inbox(limit: int = 500) -> dict:
    """Return merged inbox (received) + sent messages for thread view.

    Reads two artifacts:
      * inbox.json  -- written by the lxmf_inbox_dump.py sidecar,
                       contains everything in lxmd's messagestore
      * data/lxmf_sent.jsonl -- our own append-only sent log

    Each returned entry has `direction` set to "in" or "out" so the
    frontend can render bubbles on the correct side without needing
    to know our own LXMF address. The frontend still gets that
    address via /api/reticulum/identity for the conversation key
    (because received-from-X and sent-to-X are the same thread).
    """
    received = _read_inbox_json()
    sent = _read_sent_log()
    enrich = _read_peers_enrichment()
    contacts = read_contacts()

    def _peer_meta(h: str) -> dict:
        m = enrich.get(h, {})
        name, source = resolve_display_name(h, m, contacts)
        return {
            "peer_display_name":        name,
            "peer_display_name_source": source,
            "peer_class":               m.get("class") or "unknown",
        }

    merged: list[dict] = []

    for m in received:
        peer = m.get("source_hash") or ""
        merged.append({
            "direction":        "in",
            "hash":             m.get("hash") or "",
            "peer_hash":        peer,
            "title":            m.get("title") or "",
            "content":          m.get("content") or "",
            "timestamp":        m.get("timestamp"),
            "iso":              m.get("received_iso"),
            **_peer_meta(peer),
        })

    for s in sent:
        peer = s.get("destination_hash") or ""
        merged.append({
            "direction":        "out",
            "hash":             "",  # we don't get one back from lxmsendmsg
            "peer_hash":        peer,
            "title":            s.get("title") or "",
            "content":          s.get("content") or "",
            "timestamp":        s.get("timestamp"),
            "iso":              s.get("sent_iso"),
            **_peer_meta(peer),
        })

    # Newest-first.
    merged.sort(key=lambda x: x.get("timestamp") or 0.0, reverse=True)
    if limit > 0:
        merged = merged[:limit]

    return {
        "messages": merged,
        "count":    len(merged),
        "inbox_generated_at": _inbox_generated_at(),
    }


def read_peers_enrichment() -> dict:
    """Return the {hash: {display_name, class, is_lxmf, ...}} map
    from lxmf_peers.json.

    Returns {} on any error so endpoints degrade gracefully -- a
    missing/broken enrichment file just means the API serves
    hashes without display_names, never 500s.

    Public because other route modules (notably nodes.py for the
    Dashboard side panel) need to apply the same hash->name
    substitution to non-Reticulum-specific endpoints.
    """
    try:
        with _PEERS_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        peers = payload.get("peers", {})
        return peers if isinstance(peers, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("lxmf_peers.json read failed: %s", exc)
        return {}


# Back-compat alias for in-module callers that still reference the
# underscore-prefixed name. Remove on the next clean-up pass.
_read_peers_enrichment = read_peers_enrichment


# ── Phase 1 #2b: operator-edited contact overrides ──────────────────


_HASH_VALIDATE_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_NICKNAME_LEN = 64
_MAX_NOTES_LEN = 500


def read_contacts() -> dict:
    """Return {hash: {nickname, notes}} from data/lxmf_contacts.json.

    Returns {} on any error so callers degrade gracefully. Same pattern
    as read_peers_enrichment -- contacts are an enhancement, never a
    correctness requirement.
    """
    if not _CONTACTS_JSON.exists():
        return {}
    try:
        with _CONTACTS_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        contacts = payload.get("contacts", {})
        return contacts if isinstance(contacts, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("lxmf_contacts.json read failed: %s", exc)
        return {}


def _write_contacts(contacts: dict) -> None:
    """Atomically write contacts -- tmpfile + replace pattern."""
    _CONTACTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contacts":   contacts,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    tmp = _CONTACTS_JSON.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _CONTACTS_JSON)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except (TypeError, AttributeError):
            # Py<3.8 doesn't have missing_ok; harmless
            try: tmp.unlink()
            except OSError: pass
        raise


def resolve_display_name(hash_hex: str,
                         classifier_meta: dict | None = None,
                         contacts: dict | None = None) -> tuple[Optional[str], str]:
    """Apply the operator > classifier > placeholder priority chain.

    Returns (display_name, source) where source is one of:
      "operator"   -- name came from the operator's address book
      "classifier" -- name came from announce app_data
      "none"       -- no name available (caller should fall back to hash)

    Centralized so peers / inbox / nodes endpoints all apply the same
    rule and a future logic change (e.g. tags, "expired contact"
    handling) lives in exactly one place.
    """
    if contacts is None:
        contacts = read_contacts()
    if classifier_meta is None:
        classifier_meta = {}

    op = contacts.get(hash_hex)
    if isinstance(op, dict) and op.get("nickname"):
        return (op["nickname"], "operator")

    cl = classifier_meta.get("display_name")
    if cl:
        return (cl, "classifier")

    return (None, "none")


class ContactBody(BaseModel):
    """PUT /api/reticulum/contacts/{hash} body."""
    nickname: str = Field(..., min_length=1, max_length=_MAX_NICKNAME_LEN)
    notes:    Optional[str] = Field(default=None, max_length=_MAX_NOTES_LEN)

    @field_validator("nickname")
    @classmethod
    def _strip_nickname(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nickname cannot be blank")
        return v


@router.get("/contacts")
async def list_contacts() -> dict:
    """Return the operator's address book (hash -> {nickname, notes})."""
    contacts = read_contacts()
    return {"contacts": contacts, "count": len(contacts)}


@router.put("/contacts/{hash_hex}")
async def upsert_contact(hash_hex: str, body: ContactBody) -> dict:
    """Set or update one contact's nickname (and optional notes).

    Hash is normalised to lowercase and validated for 32 hex chars
    (standard LXMF address length). Anything else 422s.
    """
    hash_hex = hash_hex.strip().lower()
    if not _HASH_VALIDATE_RE.match(hash_hex):
        raise HTTPException(
            status_code=422,
            detail="hash must be 32 lowercase hex characters",
        )
    contacts = read_contacts()
    record: dict = {"nickname": body.nickname}
    if body.notes:
        record["notes"] = body.notes.strip()
    contacts[hash_hex] = record
    try:
        _write_contacts(contacts)
    except OSError as exc:
        logger.exception("Failed to write contacts file: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Could not persist contact: {exc}",
        )
    return {"ok": True, "hash": hash_hex, "contact": record}


@router.delete("/contacts/{hash_hex}")
async def delete_contact(hash_hex: str) -> dict:
    """Remove an operator override; display name falls back to classifier."""
    hash_hex = hash_hex.strip().lower()
    if not _HASH_VALIDATE_RE.match(hash_hex):
        raise HTTPException(status_code=422, detail="invalid hash format")
    contacts = read_contacts()
    existed = contacts.pop(hash_hex, None) is not None
    if existed:
        try:
            _write_contacts(contacts)
        except OSError as exc:
            logger.exception("Failed to write contacts file: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "hash": hash_hex, "removed": existed}


def _read_inbox_json() -> list[dict]:
    """Return the `messages` array from the sidecar's inbox.json.

    Returns an empty list if the file is missing (sidecar not yet
    running, fresh install before any messages received) or malformed
    (sidecar crashed mid-write -- shouldn't happen because the sidecar
    writes atomically, but defensive coding).
    """
    try:
        with _INBOX_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        msgs = payload.get("messages", [])
        return msgs if isinstance(msgs, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("inbox.json read failed: %s", exc)
        return []


def _inbox_generated_at() -> Optional[str]:
    """Expose the sidecar's last-write time so the UI can show staleness."""
    try:
        with _INBOX_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        gen = payload.get("generated_at")
        return gen if isinstance(gen, str) else None
    except (OSError, json.JSONDecodeError):
        return None


def _read_sent_log() -> list[dict]:
    """Parse data/lxmf_sent.jsonl line-by-line, skipping bad rows.

    JSONL not JSON so a partial line at the end (rare but possible
    if we ever crash mid-append) doesn't poison the whole log. One
    bad row = one skipped row, never a 500 on the inbox endpoint.
    """
    if not _SENT_LOG.exists():
        return []
    out: list[dict] = []
    try:
        with _SENT_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.debug("sent log read failed: %s", exc)
        return []
    return out


# ── Watch hooks for server.py's WebSocket broadcaster ────────────────


def inbox_artifact_mtimes() -> tuple[float, float, float, float]:
    """Return (inbox.json, sent log, peers.json, contacts.json) mtimes.

    Used by the server's background watcher task to decide when to
    broadcast a refresh over the WebSocket. Any path can be missing
    on a fresh install; we return 0.0 for a missing file so the
    watcher's "changed?" check sees the transition from
    0.0 -> first-real-mtime as a single change event.

    contacts.json (Phase 1 #2b) added so an operator editing a
    nickname on one browser tab causes every other connected tab
    (including the Dashboard nodes panel and Packets feed) to
    re-render with the new name within 2s.
    """
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return (
        _mtime(_INBOX_JSON), _mtime(_SENT_LOG),
        _mtime(_PEERS_JSON), _mtime(_CONTACTS_JSON),
    )


def _journal_ts_to_iso(ts: str) -> Optional[str]:
    """Convert '2026-05-16 22:18:11' (Pi local time) to a UTC ISO 8601 string.

    rnsd writes log timestamps in the host's local timezone, not UTC.
    If we tagged them UTC verbatim, the browser would compute "X ago"
    using its own local clock and the gap would be off by the host's
    UTC offset (we saw 4h on an EDT box). Treat the naive string as
    local time and convert before emitting.
    """
    try:
        naive = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        # astimezone() on a naive datetime (Py3.6+) interprets it as
        # local time and produces an aware datetime in the local zone.
        local_aware = naive.astimezone()
        return local_aware.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None
