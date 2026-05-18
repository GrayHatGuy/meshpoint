#!/usr/bin/env python3
"""LXMF inbox + peer-classification snapshot daemon.

Runs as the user that owns ~/.lxmd (typically `mp`). Does two jobs in
one process:

  1. INBOX (event-driven) -- watches ~/.lxmd/storage/messages/ via
     kernel inotify and rewrites ~/.lxmd/inbox.json atomically on
     every CREATE/MOVED_TO/DELETE event. Decodes each .lxm file via
     LXMF.LXMessage.unpack_from_file and emits a flat dict per
     message. Sub-second latency, zero CPU when idle.

  2. PEERS (periodic, every 60s) -- walks the UNION of:
       - ~/.reticulum/storage/cache/announces/  (every announce
         rnsd has ever cached -- one file per hash, comprehensive)
       - the rnsd journal (recent announces with their via-relay
         info, used to populate the relay-detection set)
     For each hash, calls RNS.Identity.recall_app_data and
     classifies the peer (lxmf / propagation / relay / rns_service
     / transport / unknown), extracting the display_name from
     LXMF announces. Result is written to ~/.lxmd/lxmf_peers.json.

Both outputs feed the Meshpoint dashboard, which reads the JSON
files but never imports RNS or LXMF itself -- preserves the
decoupling rule we set in Phase 2 #1+#2.

JSON schemas:

  inbox.json
    {
      "generated_at": "<iso8601 utc>",
      "messages": [
        {hash, source_hash, destination_hash, title, content,
         timestamp, received_iso},
        ...
      ]
    }

  lxmf_peers.json
    {
      "generated_at": "<iso8601 utc>",
      "peers": {
        "<hex_hash>": {
          "display_name": "<str or null>",
          "class":        "lxmf" | "propagation" | "relay"
                        | "rns_service" | "transport" | "unknown",
          "app_data_hex": "<hex or null>",
          "is_lxmf":      true | false
        },
        ...
      }
    }

Operational notes:
  * Both output files written atomically (tmpfile + os.replace) so
    dashboard readers never see a half-written JSON document.
  * Decode failures on individual messages or peers log at DEBUG
    and skip -- one bad row never poisons the whole snapshot.
  * inotify only fires on direct contents of the watched dir. If
    lxmd ever moves to a nested layout, the watch list needs to
    expand.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# inotify_simple is a thin ctypes wrapper around the kernel's inotify
# syscalls. Picked over pyinotify because it's actively maintained and
# has no asyncio/twisted dependencies.
try:
    from inotify_simple import INotify, flags
except ImportError:
    sys.stderr.write(
        "ERROR: inotify_simple not installed. Install with:\n"
        "  pip install --user inotify_simple\n"
    )
    sys.exit(1)

# LXMF is the heavy dep we deliberately keep OUT of Meshpoint's venv.
# It MUST already be installed in this user's environment (the same
# pip install rns lxmf that setup_rnsd.sh runs).
try:
    import LXMF  # type: ignore
except ImportError:
    sys.stderr.write(
        "ERROR: LXMF not installed in this user's environment.\n"
        "Run: pip install --user --upgrade lxmf\n"
    )
    sys.exit(1)

# RNS is used ONLY by the peer enrichment job (recall_app_data /
# recall identity from the running rnsd's shared-instance cache).
# We attach lazily inside the enrich function so a missing/broken
# RNS install doesn't prevent the inbox job from working.
try:
    import RNS  # type: ignore
    _RNS_AVAILABLE = True
except ImportError:
    _RNS_AVAILABLE = False


logging.basicConfig(
    level=os.environ.get("LXMF_DUMP_LOGLEVEL", "INFO"),
    format="[%(asctime)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("lxmf_inbox_dump")


HOME = Path(os.path.expanduser("~"))
# lxmd writes received .lxm files to ~/.lxmd/storage/messages/ (its
# "messagestore" in the LXMRouter source, but the dir is literally
# named "messages" on disk). Verified by inspecting an in-use lxmd
# install on Pi OS Bookworm.
MESSAGESTORE_DIR = HOME / ".lxmd" / "storage" / "messages"
INBOX_JSON       = HOME / ".lxmd" / "inbox.json"

# Peer enrichment artifacts. The announce cache holds one file per
# hash rnsd has ever heard -- the filename is the hash. We union
# that set with hashes seen in the journal (last 24h) to compute
# the via-relay map.
ANNOUNCE_CACHE_DIR = HOME / ".reticulum" / "storage" / "cache" / "announces"
PEERS_JSON         = HOME / ".lxmd" / "lxmf_peers.json"

# Phase 1 #6: operator-set periodic-announce preference. Owned by
# the meshpoint service user; the sidecar only READS this file. The
# announce-fire shell-outs to /opt/meshpoint/scripts/lxmf_announce.py
# (same script the dashboard "Send Now" button triggers via sudo).
ANNOUNCE_STATE_JSON = Path("/opt/meshpoint/data/lxmf_announce.json")
ANNOUNCE_SCRIPT     = Path("/opt/meshpoint/scripts/lxmf_announce.py")

# How often the peer enricher runs. 60s is a good balance: announces
# don't change minute-to-minute, but waiting 5+ minutes would mean
# operators see stale class/display-name data after a new peer arrives.
PEER_ENRICH_INTERVAL_SEC = 60.0

# How often we check whether a periodic announce is due. Doesn't need
# to be tight -- the period is operator-set in minutes, so checking
# every 30s gives at most 30s slop in fire time. Independent of the
# 60s peer-enrich tick because they could shift over time.
ANNOUNCE_POLL_INTERVAL_SEC = 30.0

# Bound the size of an LXMF display_name we'll accept from app_data.
# Anything longer is almost certainly not a real name (binary garbage
# that happens to decode as printable UTF-8).
_MAX_DISPLAY_NAME_LEN = 64


def _decode_one(path: Path) -> dict | None:
    """Decode a single .lxm file into a flat dict, or None on failure.

    The lxmd messagestore writes each message in the LXMRouter's
    on-disk format, which is read back via `LXMessage.unpack_from_file`
    -- NOT `unpack_from_bytes` (that one expects the raw wire format,
    which is different). The function takes a file HANDLE, not a path.

    Failure mode worth knowing: unpack_from_file swallows its own
    decode exceptions internally, logs them to RNS's logger, and
    returns an LXMessage with all-None fields. We detect that by
    checking source_hash post-unpack and treat a None-source message
    as a decode failure -- otherwise we'd silently surface empty
    rows in the dashboard inbox.
    """
    try:
        with path.open("rb") as f:
            msg = LXMF.LXMessage.unpack_from_file(f)
    except OSError as exc:
        logger.debug("read failed for %s: %s", path.name, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - LXMF can raise many things
        logger.debug("LXMF decode raised for %s: %s", path.name, exc)
        return None

    # unpack_from_file returns an empty-shell LXMessage on internal
    # failure (RNS logs the real error but doesn't propagate it).
    # source_hash being None is our signal that the file didn't decode.
    if msg is None or getattr(msg, "source_hash", None) is None:
        logger.debug("LXMF decode returned empty object for %s", path.name)
        return None

    # LXMF fields are bytes; coerce to hex strings / utf-8 text for JSON.
    def _hex(b: bytes | None) -> str:
        return b.hex() if isinstance(b, (bytes, bytearray)) else ""

    def _text(b) -> str:
        if isinstance(b, (bytes, bytearray)):
            try:
                return b.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""
        return str(b) if b is not None else ""

    ts = getattr(msg, "timestamp", None)
    try:
        ts_float = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts_float = None

    received_iso = (
        datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat()
        if ts_float is not None else None
    )

    return {
        "hash":             _hex(getattr(msg, "hash", None)),
        "source_hash":      _hex(getattr(msg, "source_hash", None)),
        "destination_hash": _hex(getattr(msg, "destination_hash", None)),
        "title":            _text(getattr(msg, "title", b"")),
        "content":          _text(getattr(msg, "content", b"")),
        "timestamp":        ts_float,
        "received_iso":     received_iso,
    }


def _write_atomic(payload: dict) -> None:
    """Write inbox.json atomically -- delegates to the path-generic
    _write_atomic_path defined below so both outputs share the same
    "tmpfile + chmod + rename" sequence."""
    _write_atomic_path(INBOX_JSON, payload)


def dump_inbox() -> int:
    """Re-scan messagestore + rewrite inbox.json. Returns message count.

    Cheap to call repeatedly: a few hundred messages decode in
    milliseconds on a Pi. We always re-scan the whole directory
    (vs. tracking which files we've already seen) because deletions
    from MeshChat or lxmd cleanup also need to be reflected.
    """
    messages: list[dict] = []
    if MESSAGESTORE_DIR.is_dir():
        for child in sorted(MESSAGESTORE_DIR.iterdir()):
            if not child.is_file():
                continue
            decoded = _decode_one(child)
            if decoded is not None:
                messages.append(decoded)

    # Sort newest-first; frontend reverses for thread display.
    messages.sort(
        key=lambda m: m.get("timestamp") or 0.0, reverse=True,
    )

    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "messages":     messages,
    }
    _write_atomic(payload)
    return len(messages)


# ── Peer enrichment ─────────────────────────────────────────────


_VIA_RE = __import__("re").compile(
    r"Valid announce for <[0-9a-f]+>\s+\d+\s+hops?\s+away,\s+received\s+via\s+<([0-9a-f]+)>"
)
# Same shape as the regex in src/api/routes/reticulum.py but extracts
# only the `via` hash. Kept inline (not imported from meshpoint) so
# the sidecar stays standalone.


def _decode_lxmf_name(app_data) -> str | None:
    """Pull a display_name out of an LXMF announce's app_data.

    Two formats observed in the wild:

      Format A (newer MeshChat, etc):
        msgpack array of 2 -- [display_name_bytes, propagation_node_or_nil]
        On the wire: \\x92 \\xc4 <len> <name_bytes...> \\xc0
        We decode by hand to avoid pulling in a msgpack dep.

      Format B (older clients, or raw):
        just the display_name as UTF-8 bytes, no wrapper.

    Returns the decoded name (str) or None if app_data doesn't match
    either pattern OR the decoded text looks like garbage.
    """
    if not isinstance(app_data, (bytes, bytearray)) or len(app_data) == 0:
        return None

    # Format A: \x92 (fixarray of 2) \xc4 (bin8) <len> <name> ...
    if len(app_data) >= 4 and app_data[0] == 0x92 and app_data[1] == 0xc4:
        name_len = app_data[2]
        if 0 < name_len <= _MAX_DISPLAY_NAME_LEN \
                and len(app_data) >= 3 + name_len:
            try:
                name = bytes(app_data[3:3 + name_len]).decode("utf-8")
                if name.isprintable():
                    return name
            except UnicodeDecodeError:
                pass  # fall through to Format B

    # Format B: raw UTF-8 bytes.
    if len(app_data) <= _MAX_DISPLAY_NAME_LEN:
        try:
            name = bytes(app_data).decode("utf-8")
            if name.isprintable() and len(name) >= 1:
                return name
        except UnicodeDecodeError:
            pass

    return None


def _classify_peer(hex_hash: str, app_data, identity, via_set: set) -> dict:
    """Return {class, display_name} for a single hash.

    Detection order matters -- LXMF must be checked before the
    "structured app_data => propagation" branch, because an LXMF
    display_name happens to be structured msgpack in Format A.
    """
    # 1. LXMF endpoint? (Format A or B yields a display_name)
    name = _decode_lxmf_name(app_data)
    if name is not None:
        return {"class": "lxmf", "display_name": name}

    # 2. Propagation node? Empirically these announce with a
    #    msgpack fixarray of ~7 numeric elements (capacity, cost,
    #    limits...). The leading byte 0x97 = array-of-7.
    if isinstance(app_data, (bytes, bytearray)) and len(app_data) >= 1:
        first = app_data[0]
        # 0x90..0x9f = fixarray of N elements; arrays of 4+ all-numeric
        # elements are characteristic of LXMF propagation announces.
        if 0x94 <= first <= 0x9f:
            return {"class": "propagation", "display_name": None}

    # 3. Relay? No LXMF/propagation classification but appears as a
    #    `via` target for at least one other announce.
    if hex_hash in via_set:
        return {"class": "relay", "display_name": None}

    # 4. RNS service: we have an identity AND some non-empty app_data
    #    that we couldn't classify. Means it's announcing SOMETHING
    #    (Nomadnet page, NSE service, custom) -- just not LXMF.
    if identity is not None and isinstance(app_data, (bytes, bytearray)) \
            and len(app_data) > 0:
        return {"class": "rns_service", "display_name": None}

    # 5. Transport: no identity cached. Typically a multi-hop
    #    intermediary we've only ever seen as a `via` hash.
    if identity is None:
        return {"class": "transport", "display_name": None}

    # 6. Anything else.
    return {"class": "unknown", "display_name": None}


def _journal_via_set() -> set:
    """Scrape the last 24h of rnsd journal for `received via <hash>`.

    Each hash that appears as a relay target is added to the set.
    Used by the classifier to mark transport nodes that are actively
    relaying traffic (vs. silent transport hashes we've never seen
    in action).
    """
    import subprocess
    via: set = set()
    try:
        result = subprocess.run(
            ["journalctl", "-u", "rnsd.service", "--no-pager",
             "--since", "24 hours ago"],
            capture_output=True, text=True, timeout=5.0,
        )
        for line in result.stdout.splitlines():
            m = _VIA_RE.search(line)
            if m:
                via.add(m.group(1))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("journalctl scrape for via-set failed: %s", exc)
    return via


def _collect_candidate_hashes() -> list:
    """Union of announce-cache filenames + journal-mentioned hashes.

    Belt and suspenders: the announce cache catches everything rnsd
    has ever heard (deep history); the journal scrape catches the
    same plus the via-relay info we need anyway, so it's nearly
    free to fold in.
    """
    candidates: set = set()

    # Announce cache: filename = hash (lowercase hex).
    if ANNOUNCE_CACHE_DIR.is_dir():
        try:
            for child in ANNOUNCE_CACHE_DIR.iterdir():
                if not child.is_file():
                    continue
                name = child.name.lower()
                if len(name) >= 16 and all(c in "0123456789abcdef" for c in name):
                    candidates.add(name)
        except OSError as exc:
            logger.debug("announce cache iter failed: %s", exc)

    # Journal scrape: pull BOTH the announce-target hash AND any
    # `via <hash>` relay. Without the via capture, pure transport
    # nodes that only ever appear as relays (never as their own
    # announce target) would be silently dropped from enrichment
    # and their "relay" classification would never get computed.
    import subprocess, re as _re
    target_re = _re.compile(r"Valid announce for <([0-9a-f]+)>")
    via_re    = _re.compile(r"received\s+via\s+<([0-9a-f]+)>")
    try:
        result = subprocess.run(
            ["journalctl", "-u", "rnsd.service", "--no-pager",
             "--since", "24 hours ago"],
            capture_output=True, text=True, timeout=5.0,
        )
        for line in result.stdout.splitlines():
            m = target_re.search(line)
            if m:
                candidates.add(m.group(1).lower())
            v = via_re.search(line)
            if v:
                candidates.add(v.group(1).lower())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return sorted(candidates)


_rns_attached = False


def _ensure_rns_attached() -> bool:
    """Attach to the running rnsd via shared-instance socket (idempotent).

    Called lazily on the first enrich tick so a missing RNS install
    doesn't kill the inbox dumper. Returns True if we have a usable
    RNS handle, False otherwise.
    """
    global _rns_attached
    if not _RNS_AVAILABLE:
        return False
    if _rns_attached:
        return True
    try:
        # No configdir => default ~/.reticulum/, which has
        # share_instance = Yes; RNS auto-detects the running rnsd's
        # local socket and piggybacks on it (no second radio).
        RNS.Reticulum(loglevel=0)
        _rns_attached = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not attach to RNS for enrichment: %s", exc)
        return False


def enrich_peers() -> int:
    """Classify every known hash and write lxmf_peers.json.

    Returns the count of classified peers (== the JSON's `peers`
    dict length). Idempotent and bounded -- typical Pi installs
    see hundreds of cached announces, decoded in well under a
    second.
    """
    if not _ensure_rns_attached():
        return 0

    via_set = _journal_via_set()
    candidates = _collect_candidate_hashes()

    peers: dict = {}
    for hex_hash in candidates:
        try:
            h = bytes.fromhex(hex_hash)
        except ValueError:
            continue
        try:
            app_data = RNS.Identity.recall_app_data(h)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall_app_data failed for %s: %s", hex_hash, exc)
            app_data = None
        try:
            identity = RNS.Identity.recall(h)
        except Exception:  # noqa: BLE001
            identity = None

        cls = _classify_peer(hex_hash, app_data, identity, via_set)
        peers[hex_hash] = {
            "display_name": cls["display_name"],
            "class":        cls["class"],
            "is_lxmf":      cls["class"] == "lxmf",
            "app_data_hex": app_data.hex() if isinstance(app_data, (bytes, bytearray)) else None,
        }

    _write_atomic_path(PEERS_JSON, {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "peers":        peers,
    })
    return len(peers)


def _write_atomic_path(target: Path, payload: dict) -> None:
    """Same atomic-write contract as inbox.json's _write_atomic, for
    arbitrary target paths. Kept as a separate helper so future
    sidecar outputs can reuse it without duplicating the dance."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="." + target.name + ".", suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_announce_state() -> dict:
    """Return the operator's announce preference + last-fire metadata.

    Returns sensible defaults (period=0 disabled, last_announce_at=None)
    on missing/malformed file. Read-only from the sidecar's perspective
    -- the dashboard owns writes.
    """
    default = {
        "period_minutes":   0,
        "last_announce_at": None,
        "last_announce_ok": None,
    }
    if not ANNOUNCE_STATE_JSON.exists():
        return default
    try:
        with ANNOUNCE_STATE_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return {**default, **(payload or {})}
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("announce state read failed: %s", exc)
        return default


def _write_announce_outcome(success: bool) -> None:
    """Best-effort persist of last_announce_at + last_announce_ok.

    Cross-user perms: this file is created by the dashboard
    (running as `meshpoint`) on Save Announce / Send Now. As of
    the Phase 1 #6 bug fix the dashboard chmods it 0666 so we (the
    sidecar, running as `mp`) can rewrite it in-place. We can't
    do the usual tmp-then-rename atomic write here because that
    needs write access on the parent dir (/opt/meshpoint/data),
    which is meshpoint-owned 0755 -- mp can't create new files
    there. Direct in-place open(w) works because the file
    already exists and is mode 0666.

    The downside is a brief truncation window where a concurrent
    reader could see an empty file. The dashboard's _read_announce_state
    handles JSONDecodeError by returning defaults, so the worst
    case is one stale read -- recovers on the next poll.
    """
    try:
        state = _read_announce_state()
        state["last_announce_at"] = __import__("time").time()
        state["last_announce_ok"] = bool(success)
        # Direct overwrite -- see docstring re: no atomic dance.
        with ANNOUNCE_STATE_JSON.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("Could not persist announce outcome: %s", exc)


def maybe_fire_periodic_announce() -> None:
    """If the saved period has elapsed since last announce, fire one.

    Triggered from the main loop at ANNOUNCE_POLL_INTERVAL_SEC
    cadence. period=0 means "auto-announce disabled" -- no-op.

    Uses the same scripts/lxmf_announce.py that the dashboard
    "Send Now" button shells out to, so there's exactly one code
    path for "fire an announce" (audit + tweak in one place).
    """
    state = _read_announce_state()
    period_min = int(state.get("period_minutes") or 0)
    if period_min <= 0:
        return  # auto-announce off

    import time as _time
    last = state.get("last_announce_at") or 0.0
    elapsed = _time.time() - float(last)
    if elapsed < period_min * 60.0:
        return  # not yet due

    if not ANNOUNCE_SCRIPT.is_file() or not os.access(ANNOUNCE_SCRIPT, os.X_OK):
        logger.warning(
            "Periodic announce due but %s missing/not executable",
            ANNOUNCE_SCRIPT,
        )
        return

    import subprocess
    logger.info(
        "Periodic announce due (period=%dm, elapsed=%.0fs); firing",
        period_min, elapsed,
    )
    try:
        result = subprocess.run(
            [str(ANNOUNCE_SCRIPT)],
            capture_output=True, text=True, timeout=15.0,
        )
        ok = (result.returncode == 0)
        if not ok:
            logger.warning(
                "Periodic announce failed (rc=%d): %s",
                result.returncode,
                (result.stderr or result.stdout or "")[:200],
            )
        _write_announce_outcome(success=ok)
    except subprocess.TimeoutExpired:
        logger.warning("Periodic announce timed out")
        _write_announce_outcome(success=False)
    except Exception:  # noqa: BLE001
        logger.exception("Periodic announce raised")
        _write_announce_outcome(success=False)


def main() -> int:
    # Make sure the messagestore exists -- lxmd creates it lazily on
    # first received message, so on a brand-new install the dir may
    # not be there yet. We mkdir it ourselves so inotify has something
    # to watch and the dashboard sees an (empty) snapshot immediately.
    MESSAGESTORE_DIR.mkdir(parents=True, exist_ok=True)

    # Initial snapshot before entering the event loop. Without this,
    # the inbox endpoint would 404 until the first message arrived.
    count = dump_inbox()
    logger.info("Initial snapshot: %d message(s) -> %s", count, INBOX_JSON)

    # Initial peer enrichment pass. Best-effort -- if RNS isn't
    # importable (e.g. inotify_simple installed but rns missing for
    # some reason) we log and continue without peer enrichment.
    try:
        peer_count = enrich_peers()
        logger.info("Initial peer enrichment: %d peer(s) -> %s",
                    peer_count, PEERS_JSON)
    except Exception:
        logger.exception("Initial peer enrichment failed")

    import time as _time
    last_enrich  = _time.time()
    last_announce_poll = 0.0   # force first check on startup

    ino = INotify()
    # CREATE catches `touch new.lxm`-style writes; MOVED_TO catches
    # the more-common `write to tmp then rename` pattern lxmd uses.
    # DELETE/MOVED_FROM matter too because we re-scan from scratch and
    # need to drop entries the user deleted in MeshChat.
    watch_flags = (
        flags.CREATE | flags.MOVED_TO | flags.DELETE | flags.MOVED_FROM
    )
    ino.add_watch(str(MESSAGESTORE_DIR), watch_flags)
    logger.info("Watching %s (inotify)", MESSAGESTORE_DIR)

    # Graceful shutdown on SIGTERM (systemd stop) and SIGINT (Ctrl-C).
    running = True

    def _stop(signum, _frame):
        nonlocal running
        logger.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running:
        # read() blocks in the kernel until at least one event fires
        # OR a signal interrupts us. timeout=1000ms is a safety belt
        # so the SIGTERM handler can flip `running` and we notice
        # within a second AND so the peer-enrich cadence check
        # below runs roughly every second when idle.
        try:
            events = ino.read(timeout=1000)
        except InterruptedError:
            continue

        if events:
            # Coalesce: if 5 messages arrived in the same kernel
            # batch we only need ONE re-dump, not 5.
            try:
                count = dump_inbox()
                logger.info(
                    "Re-dumped after %d event(s): %d message(s)",
                    len(events), count,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Dump failed: %s", exc)

        # Peer enrichment cadence check. Runs every
        # PEER_ENRICH_INTERVAL_SEC regardless of inotify activity,
        # so a quiet messagestore doesn't starve the peers JSON.
        now = _time.time()
        if now - last_enrich >= PEER_ENRICH_INTERVAL_SEC:
            last_enrich = now
            try:
                peer_count = enrich_peers()
                logger.debug(
                    "Periodic peer enrichment: %d peer(s)", peer_count,
                )
            except Exception:
                logger.exception("Periodic peer enrichment failed")

        # Phase 1 #6: check if a periodic auto-announce is due.
        # Independent from enrich cadence -- announce period is
        # operator-set in minutes (much coarser than peer-enrich),
        # so we just poll the state file every 30s. The fire
        # function itself is a no-op when period=0 or not yet due.
        if now - last_announce_poll >= ANNOUNCE_POLL_INTERVAL_SEC:
            last_announce_poll = now
            try:
                maybe_fire_periodic_announce()
            except Exception:
                logger.exception("Periodic announce check failed")

    logger.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
