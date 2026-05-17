"""Reticulum frame decoder for packets received via RNode.

Parses the binary Reticulum frame header that arrives as the CMD_DATA
payload from the RNode KISS interface and maps it to a Packet object.

Reticulum frame layout (from RNS/Packet.py):

  HEADER_1 (no transport):
    [0]      flags  (1 byte)
    [1]      hops   (1 byte)
    [2:18]   destination hash (16 bytes, truncated SHA-256)
    [18]     context (1 byte)
    [19:]    ciphertext payload

  HEADER_2 (with transport relay):
    [0]      flags
    [1]      hops
    [2:18]   transport ID (16 bytes)
    [18:34]  destination hash (16 bytes)
    [34]     context
    [35:]    ciphertext payload

Flags byte bitmasks:
  bit 6     : header_type  (0 = HEADER_1, 1 = HEADER_2)
  bit 5     : context_flag
  bit 4     : transport_type
  bits 3-2  : destination_type
  bits 1-0  : packet_type

_PTYPE_DATA         = 0x00
_PTYPE_ANNOUNCE     = 0x01
_PTYPE_LINK_REQUEST = 0x02
_PTYPE_PROOF        = 0x03  
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from src.models.node import Node
from src.models.packet import Packet, PacketType, Protocol
from src.models.signal import SignalMetrics
from src.models.telemetry import Telemetry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reticulum constants
# ---------------------------------------------------------------------------
TRUNCATED_HASH_LEN = 16          # bytes (128-bit truncated SHA-256)

HEADER_TYPE_MASK   = 0b01000000  # bit 6
DEST_TYPE_MASK     = 0b00001100  # bits 3-2
PACKET_TYPE_MASK   = 0b00000011  # bits 1-0

# Reticulum frames captured via the SX1302 concentrator path arrive
# with a one-byte radio-driver prefix at raw[0] BEFORE the standard
# RNS header begins. Empirically verified by comparing decoded
# transport_id and dest_hash against known peer hashes from the
# sidecar's lxmf_peers.json: every field was shifted right by exactly
# one byte (e.g. decoded transport `01c7b745…79` vs real
# `c7b745…792e` -- the `01` is the hops byte and the trailing `2e`
# slid into the next field). All offsets here therefore start at
# raw[FRAME_PREFIX_LEN+0]. If a future driver fix removes the prefix
# byte, set FRAME_PREFIX_LEN = 0 to revert.
FRAME_PREFIX_LEN = 1

HEADER_1_MIN_LEN = FRAME_PREFIX_LEN + 19  # prefix + flags + hops + 16B hash + context
HEADER_2_MIN_LEN = FRAME_PREFIX_LEN + 35  # prefix + flags + hops + 16B transport + 16B hash + context

# Reticulum packet_type values (bits 1-0 of flags)
_PTYPE_DATA         = 0x00
_PTYPE_ANNOUNCE     = 0x01
_PTYPE_LINK_REQUEST = 0x02
_PTYPE_PROOF        = 0x03

_PACKET_TYPE_MAP: dict[int, PacketType] = {
    _PTYPE_DATA:         PacketType.UNKNOWN,    # encrypted data; type unknowable
    _PTYPE_ANNOUNCE:     PacketType.NODEINFO,   # identity advertisement
    _PTYPE_LINK_REQUEST: PacketType.ROUTING,
    _PTYPE_PROOF:        PacketType.ROUTING,
}


class RnodeDecoder:
    """Parses raw Reticulum frames into Packet objects.

    Source IDs are not extractable from the LoRa frame header (they are
    inside the encrypted payload at the application layer), so source_id
    is set to ``"unknown"`` for all packets in this first pass.
    """

    def decode(
        self,
        raw: bytes,
        signal: Optional[SignalMetrics] = None,
    ) -> Optional[Packet]:
        if len(raw) < HEADER_1_MIN_LEN:
            logger.debug(
                "Reticulum frame too short: %d bytes (min %d)",
                len(raw), HEADER_1_MIN_LEN,
            )
            return None

        # Skip the radio-driver prefix byte (see FRAME_PREFIX_LEN
        # comment above). All subsequent indexing is relative to the
        # start of the real RNS header.
        p = FRAME_PREFIX_LEN

        flags       = raw[p + 0]
        hops        = raw[p + 1]
        header_type = (flags & HEADER_TYPE_MASK) >> 6
        packet_type = flags & PACKET_TYPE_MASK

        if header_type == 0:
            # HEADER_1: no transport relay field
            dest_hash    = raw[p + 2:p + 2 + TRUNCATED_HASH_LEN].hex()
            payload      = raw[HEADER_1_MIN_LEN:]
            transport_id = None
        else:
            # HEADER_2: includes a transport relay ID
            if len(raw) < HEADER_2_MIN_LEN:
                logger.debug(
                    "Reticulum HEADER_2 frame too short: %d bytes (min %d)",
                    len(raw), HEADER_2_MIN_LEN,
                )
                return None
            transport_id = raw[p + 2:p + 2 + TRUNCATED_HASH_LEN].hex()
            dest_hash    = raw[p + 2 + TRUNCATED_HASH_LEN:
                              p + 2 + 2 * TRUNCATED_HASH_LEN].hex()
            payload      = raw[HEADER_2_MIN_LEN:]

        pkt_type = _PACKET_TYPE_MAP.get(packet_type, PacketType.UNKNOWN)

        decoded: dict[str, Any] = {
            "dest_hash":   dest_hash,
            "hops":        hops,
            "header_type": header_type,
            "packet_type": packet_type,
        }
        if transport_id:
            decoded["transport_id"] = transport_id
        # Surface a peek at the ciphertext for diagnostics only
        if payload:
            decoded["payload_hex_preview"] = payload[:16].hex()

        # Source extraction:
        # - ANNOUNCE packets broadcast the announcer's OWN destination,
        #   so source == dest. The dashboard can therefore look up the
        #   announcer in lxmf_peers.json and surface their display_name
        #   in the Source column instead of literal "unknown".
        # - All other packet types carry no source field in the LoRa
        #   frame header (it lives inside the encrypted payload), so
        #   "unknown" is the most honest value. The frontend renders
        #   that as the operator-facing "unknown" placeholder rather
        #   than chopping it as if it were an 8-char Meshtastic node id.
        source_id = dest_hash if packet_type == _PTYPE_ANNOUNCE else "unknown"

        return Packet(
            packet_id=uuid4().hex[:8],
            source_id=source_id,
            destination_id=dest_hash,
            protocol=Protocol.RETICULUM,
            packet_type=pkt_type,
            hop_limit=hops,
            decoded_payload=decoded,
            signal=signal,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Protocol-conformance methods (called by coordinator for all protocols)
    # ------------------------------------------------------------------

    def extract_node_update(self, packet: Packet) -> Optional[Node]:
        """Node discovery from ANNOUNCE frames only."""
        if packet.packet_type != PacketType.NODEINFO:
            return None
        if not packet.decoded_payload:
            return None
        dest_hash = packet.decoded_payload.get("dest_hash", packet.destination_id)
        return Node(
            node_id=dest_hash,
            protocol=Protocol.RETICULUM.value,
            last_heard=packet.timestamp,
            latest_signal=packet.signal,
        )

    def extract_telemetry(self, packet: Packet) -> Optional[Telemetry]:
        # Reticulum telemetry requires application-layer decryption; skip for now.
        return None
