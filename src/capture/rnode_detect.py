"""Auto-detect RNode devices on USB serial ports.

Scans /dev/ttyUSB* and /dev/ttyACM* for devices that respond to the
RNode KISS probe command.  Mirrors the pattern used by meshcore_usb_detect.py.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_USB_PATTERNS      = ["/dev/ttyUSB*", "/dev/ttyACM*"]
_SETTLE_SECONDS    = 2.0
_PROBE_TIMEOUT     = 5.0
_READ_POLL_SECONDS = 0.05


def find_serial_candidates(
    exclude_ports: frozenset[str] = frozenset(),
) -> list[str]:
    """Return USB serial ports that could be RNode devices."""
    candidates: list[str] = []
    for pattern in _USB_PATTERNS:
        candidates.extend(glob.glob(pattern))
    return sorted(p for p in candidates if p not in exclude_ports)


def _probe_port(port: str, baud: int) -> bool:
    """Open *port*, send a KISS CMD_DETECT frame, and check for any response.

    Runs synchronously — call via ``asyncio.to_thread``.
    """
    try:
        import serial
        from src.hal.rnode_driver import FEND, CMD_DETECT
    except ImportError:
        logger.debug("pyserial not installed, cannot probe %s", port)
        return False

    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=0,
            xonxoff=False,
            rtscts=False,
        )
    except serial.SerialException as exc:
        logger.debug("Cannot open %s: %s", port, exc)
        return False

    try:
        time.sleep(_SETTLE_SECONDS)
        # Send [FEND CMD_DETECT FEND]
        ser.write(bytes([FEND, CMD_DETECT, FEND]))

        deadline = time.monotonic() + _PROBE_TIMEOUT
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = ser.read(64)
            if chunk:
                buf.extend(chunk)
                # Any valid FEND-delimited frame with >=2 bytes is enough
                if buf.count(FEND) >= 2:
                    logger.debug("RNode probe succeeded on %s", port)
                    return True
            time.sleep(_READ_POLL_SECONDS)

        logger.debug("RNode probe timed out on %s", port)
        return False

    except Exception as exc:
        logger.debug("RNode probe error on %s: %s", port, exc)
        return False

    finally:
        try:
            ser.close()
        except Exception:
            pass


async def probe_rnode_port(
    port: str,
    baud: int = 115200,
    timeout: float = _PROBE_TIMEOUT + _SETTLE_SECONDS + 1.0,
) -> bool:
    """Async wrapper around ``_probe_port``."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_port, port, baud),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.debug("RNode probe async timeout on %s", port)
        return False


async def detect_rnode_port(
    exclude_ports: frozenset[str] = frozenset(),
    baud: int = 115200,
) -> Optional[str]:
    """Return the first USB serial port with a responding RNode device."""
    candidates = find_serial_candidates(exclude_ports)
    if not candidates:
        return None

    for port in candidates:
        logger.debug("Probing %s for RNode device...", port)
        if await probe_rnode_port(port, baud):
            logger.info("RNode device detected on %s", port)
            return port

    return None
