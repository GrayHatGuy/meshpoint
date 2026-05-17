#!/usr/bin/env python3
"""LXMF inbox -> JSON snapshot daemon.

Runs as the user that owns ~/.lxmd (typically `mp`). Watches the lxmd
messagestore for new `.lxm` files via Linux inotify, and on every
change rewrites ~/.lxmd/inbox.json with a flat, decoded view of every
message currently in the store.

The Meshpoint dashboard (running as a different user, `meshpoint`)
then reads inbox.json -- it never imports LXMF itself. This is the
same decoupling rule we used for /api/reticulum/peers (journal scrape
instead of `import RNS`): keep the heavy messaging stack confined to
the mp user's venv so dashboard upgrades don't pin LXMF versions and
vice-versa.

JSON schema written to inbox.json:
    {
        "generated_at": "<iso8601 utc>",
        "messages": [
            {
                "hash":             "<hex>",
                "source_hash":      "<hex>",
                "destination_hash": "<hex>",
                "title":            "<utf-8 string or empty>",
                "content":          "<utf-8 string>",
                "timestamp":        <float unix epoch>,
                "received_iso":     "<iso8601 utc>"
            },
            ...
        ]
    }

A new snapshot is written:
  * once on startup (so the file exists even if no events have fired),
  * on every CREATE / MOVED_TO event under the messagestore dir,
  * never on a timer -- pure event-driven, sleeps in the kernel
    when idle (zero CPU).

Operational notes:
  * The output file is written atomically (tmpfile + os.replace) so
    readers never see a half-written JSON document.
  * If a single `.lxm` file fails to decode (truncated, format change,
    permissions), we log and skip it -- one bad message must never
    poison the whole snapshot.
  * inotify only fires on direct contents of the watched dir. If lxmd
    ever moves to a nested layout, the watch list needs to expand.
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
INBOX_JSON = HOME / ".lxmd" / "inbox.json"


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
    """Write inbox.json atomically so readers never see a partial file.

    Strategy: serialize to a tmpfile in the same directory (so
    os.replace is a same-filesystem rename), chmod 644 so the
    meshpoint user can read it, then atomic-rename onto the final
    path. Old readers either see the previous full file or the new
    full file -- never a half-written one.
    """
    INBOX_JSON.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".inbox.", suffix=".json.tmp", dir=str(INBOX_JSON.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        # World-readable so the meshpoint user can fetch it without
        # needing to chmod the whole .lxmd dir.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, INBOX_JSON)
    except Exception:
        # Best-effort cleanup; we don't want orphan tmpfiles piling up
        # if the rename failed for some weird reason.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
        # within a second.
        try:
            events = ino.read(timeout=1000)
        except InterruptedError:
            continue
        if not events:
            continue
        # Coalesce: if 5 messages arrived in the same kernel batch we
        # only need ONE re-dump, not 5.
        try:
            count = dump_inbox()
            logger.info(
                "Re-dumped after %d event(s): %d message(s)",
                len(events), count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Dump failed: %s", exc)

    logger.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
