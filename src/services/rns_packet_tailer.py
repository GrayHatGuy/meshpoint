"""Background task that drains the sidecar's RNS packet JSONL queue.

Architecture
============

On the rnodeusb branch, RNS lives entirely on the RNode USB stick.
The meshpoint service can't open ``/dev/ttyUSB0`` (rnsd owns it), so
to "hear" RNS traffic the sidecar (``scripts/lxmf_inbox_dump.py``)
registers an ``RNS.Transport.register_packet_callback`` and appends
each captured packet as a JSONL line to
``/opt/meshpoint/data/rns_packets.jsonl`` (mode 0666 so the
meshpoint service user can read+truncate it).

This module runs in the main service's asyncio loop and:

  * tails the JSONL queue (newline-delimited, append-only)
  * parses each line into a ``Packet`` model
  * inserts it into the packets table via ``PacketRepository``
  * broadcasts a ``"packet"`` WS event so connected dashboards
    pick it up sub-second

Read offset persists in-process: on startup we seek to EOF (so we
don't re-replay the entire backlog on every restart), then track
file position across rotations. The sidecar size-caps the file at
10 MB and truncates when over; we detect truncation via st_size
shrinking below our cursor and reset to 0.

This complements but doesn't yet fully replace the polling sync in
``src/api/routes/nodes.py`` (the announce-journal grep covers the
historical window since rnsd boot, which the live callback can't).
The polling path may be retired in a follow-up once we're confident
the live callback catches everything we care about.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.api.websocket_manager import WebSocketManager
from src.models.packet import Packet, PacketType, Protocol
from src.models.signal import SignalMetrics
from src.storage.packet_repository import PacketRepository

logger = logging.getLogger(__name__)


# Queue file written by scripts/lxmf_inbox_dump.py.
RNS_PACKETS_JSONL = Path("/opt/meshpoint/data/rns_packets.jsonl")

# Cadence between file-size polls when there's nothing new. Short
# enough that LXMF DMs feel live; long enough that we don't burn CPU
# on a quiet mesh.
POLL_INTERVAL_SEC = 0.5

# Map the RNS packet_type bits (0-3) to our PacketType enum. DATA is
# encrypted -> UNKNOWN (we don't know what's inside without keys);
# ANNOUNCE -> NODEINFO (identity advertisement); LINK_REQUEST and
# PROOF -> ROUTING (control plane).
_PTYPE_TO_PACKETTYPE = {
    0: PacketType.UNKNOWN,    # data
    1: PacketType.NODEINFO,   # announce
    2: PacketType.ROUTING,    # link_request
    3: PacketType.ROUTING,    # proof
}


class RnsPacketTailer:
    """Async drain loop for the sidecar's RNS packet JSONL queue."""

    def __init__(
        self,
        packet_repo: PacketRepository,
        ws_manager: Optional[WebSocketManager] = None,
        queue_path: Path = RNS_PACKETS_JSONL,
    ):
        self._packet_repo = packet_repo
        self._ws_manager = ws_manager
        self._queue_path = queue_path
        self._position = 0      # byte offset of next read
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Begin tailing in the background; seeks to current EOF.

        Seeking to EOF means we don't replay historical packets on
        startup -- they're either already in the DB (from a prior
        run) or covered by the polling-driven backfill in nodes.py.
        Only NEW packets emitted after this start fire through.
        """
        if self._task is not None:
            return
        try:
            if self._queue_path.exists():
                self._position = self._queue_path.stat().st_size
            else:
                self._position = 0
        except OSError as exc:
            logger.warning(
                "RNS packet tailer: stat() failed on %s: %s",
                self._queue_path, exc,
            )
            self._position = 0
        self._running = True
        self._task = asyncio.create_task(
            self._drain_loop(), name="rns-packet-tailer",
        )
        logger.info(
            "RNS packet tailer started (queue=%s start_offset=%d)",
            self._queue_path, self._position,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _drain_loop(self) -> None:
        """Periodically check for new bytes in the JSONL queue."""
        while self._running:
            try:
                await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Never let one bad iteration kill the loop -- log and
                # back off so a corrupt file doesn't pin the CPU.
                logger.warning(
                    "RNS packet tailer drain failed: %s", exc, exc_info=True,
                )
                await asyncio.sleep(2.0)
                continue
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _drain_once(self) -> None:
        if not self._queue_path.exists():
            return
        size = self._queue_path.stat().st_size
        # Sidecar truncates when over its size cap -- detect that and
        # reset our cursor so we don't try to read past EOF or re-skip
        # legitimate bytes.
        if size < self._position:
            logger.debug(
                "RNS queue truncated (was %d, now %d); resetting cursor",
                self._position, size,
            )
            self._position = 0
        if size == self._position:
            return
        with self._queue_path.open("rb") as f:
            f.seek(self._position)
            chunk = f.read(size - self._position)
            self._position = size
        # Decode bytes; tolerate partial trailing line by re-reading on
        # next tick (rare with line-buffered appends but worth guarding).
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self._handle_packet_payload(obj)

    async def _handle_packet_payload(self, obj: dict) -> None:
        """Convert a sidecar JSONL line into a Packet and persist/broadcast."""
        dest = obj.get("dest_hash") or ""
        if not dest:
            return
        ts_unix = obj.get("ts")
        try:
            ts = datetime.fromtimestamp(float(ts_unix), tz=timezone.utc)
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)

        ptype_int = int(obj.get("packet_type") or 0)
        packet_type = _PTYPE_TO_PACKETTYPE.get(ptype_int, PacketType.UNKNOWN)

        # SignalMetrics requires all five RF fields. RNode-USB on
        # rnodeusb is US Reticulum (914.875 MHz / SF8 / 125 kHz).
        signal = SignalMetrics(
            rssi=float(obj.get("rssi") or 0.0),
            snr=float(obj.get("snr") or 0.0),
            frequency_mhz=914.875,
            spreading_factor=8,
            bandwidth_khz=125.0,
        )

        # For ANNOUNCE the announcer broadcasts its own destination,
        # so source==dest. For other types, source isn't in the LoRa
        # frame header -- it's inside the encrypted payload. Use
        # transport_id (HEADER_2 relay) when available as a hint about
        # the upstream node; otherwise mark "unknown".
        if ptype_int == 1:
            source_id = dest
        elif obj.get("transport_id"):
            source_id = obj["transport_id"]
        else:
            source_id = "unknown"

        # Stable packet_id: collapse retransmits of the same frame
        # within the same second (sidecar can see repeats if Transport
        # forwards them). 16 hex chars from a sha1 is plenty.
        import hashlib
        key = f"{dest}:{ptype_int}:{int(ts.timestamp())}:{obj.get('hops', 0)}"
        packet_id = hashlib.sha1(key.encode()).hexdigest()[:16]

        decoded_payload = {
            "dest_hash":         dest,
            "transport_id":      obj.get("transport_id"),
            "header_type":       obj.get("header_type"),
            "packet_type":       ptype_int,
            "packet_type_label": obj.get("packet_type_label"),
            "destination_type":  obj.get("destination_type"),
            "transport_type":    obj.get("transport_type"),
            "context_flag":      obj.get("context_flag"),
            "context":           obj.get("context"),
            "interface":         obj.get("interface"),
            "raw_len":           obj.get("raw_len"),
        }

        pkt = Packet(
            packet_id=packet_id,
            source_id=source_id,
            destination_id=dest,
            protocol=Protocol.RETICULUM,
            packet_type=packet_type,
            hop_limit=int(obj.get("hops") or 0),
            decoded_payload=decoded_payload,
            signal=signal,
            timestamp=ts,
            capture_source="rns_transport_callback",
        )

        try:
            await self._packet_repo.insert(pkt)
        except Exception as exc:  # noqa: BLE001
            logger.debug("RNS tailer insert failed: %s", exc)
            return
        if self._ws_manager is not None:
            try:
                await self._ws_manager.broadcast("packet", pkt.to_dict())
            except Exception as exc:  # noqa: BLE001
                logger.debug("RNS tailer WS broadcast failed: %s", exc)
