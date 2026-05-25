"""Adapter from RNode RawCapture payloads to Packet objects.

Unlike the MeshCore adapter (which deserialises JSON event envelopes),
RNode payloads are raw binary Reticulum frames -- so this adapter simply
delegates to RnodeDecoder.decode() with no intermediate parsing step.
"""

from __future__ import annotations

from typing import Optional

from src.decode.rnode_decoder import RnodeDecoder
from src.models.packet import Packet
from src.models.signal import SignalMetrics

_decoder = RnodeDecoder()


def adapt_frame(
    payload: bytes,
    signal: Optional[SignalMetrics] = None,
) -> Optional[Packet]:
    """Convert a raw Reticulum frame bytes into a Packet.

    Returns None if the frame is malformed or too short.
    """
    return _decoder.decode(payload, signal=signal)
