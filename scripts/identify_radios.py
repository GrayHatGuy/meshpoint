#!/usr/bin/env python3
"""Functional protocol probe -- identify USB-serial radio devices.

Motivation: on hardware where the USB metadata is non-unique
(notably Heltec V2/V3 boards, all CP2102 chips that report
ID_SERIAL_SHORT=0001), passive identification by udev attributes
fails. We can't tell an RNode dongle from a MeshCore companion
or a Meshtastic node just by looking at /sys descriptors. The
only definitive answer is to OPEN each candidate serial port,
send protocol-specific probe bytes, and classify by the reply.

This script:
  1. Enumerates candidate ports (/dev/ttyUSB*, /dev/ttyACM*).
  2. Skips ports that another process already holds (don't fight
     for the fd; that's how you wedge a running rnsd / meshpoint).
  3. For each free port, in turn:
        a. RNode probe   (KISS FEND CMD_DETECT FEND -> KISS reply)
        b. Meshtastic    (0x94 0xC3 header, want_config_id -> proto reply)
        c. MeshCore      (text/CBOR newline query -> structured reply)
     The probes are sent sequentially; whichever pattern matches
     first wins, port is closed, next port begins.
  4. Emits a JSON map on stdout that setup_rnsd.sh can consume.

Output schema:
    {
        "scanned":    ["/dev/ttyUSB0", "/dev/ttyUSB1"],
        "busy":       ["/dev/ttyACM0"],      # held by another process
        "rnode":      "/dev/ttyUSB0",        # or null
        "meshcore":   "/dev/ttyUSB1",        # or null
        "meshtastic": null,
        "unknown":    [],                    # opened OK, no probe matched
        "errors":     {"<port>": "<reason>"} # open/probe error per port
    }

If multiple ports return the same protocol, the FIRST matching port
wins for that slot and subsequent matches go into "unknown" -- a
warning is added to "errors". Operators will need to disambiguate
those by hand (rare with two boards, more common with a swarm).

Usage:
    sudo -u mp scripts/identify_radios.py [--ports /dev/ttyUSB0 /dev/ttyUSB1]
    sudo -u mp scripts/identify_radios.py --baud 115200 --timeout 0.6

Exit code 0 always (so setup scripts can read JSON unconditionally);
the JSON tells the caller what was found or not.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Optional

try:
    import serial  # pyserial
except ImportError:
    sys.stderr.write(
        "ERROR: pyserial not installed. Install with:\n"
        "  pip install --user --break-system-packages pyserial\n"
        "(Already a transitive dep of rns + lxmf, so it's normally present.)\n"
    )
    sys.exit(1)


# ── KISS / RNode probe ───────────────────────────────────────────────
# KISS framing (RFC 1055-ish): FEND=0xC0 wraps each frame. RNode adds
# its own command codes inside. CMD_DETECT (0x46 in the RNode firmware
# convention) prompts the device to identify itself; reply starts with
# FEND too. Even an UNRECOGNISED command on an RNode triggers a frame
# response (the firmware echoes/rejects), so any FEND-bracketed reply
# is a strong RNode signal.
_KISS_FEND       = 0xC0
_KISS_CMD_DETECT = 0x46  # RNode-specific detect; firmware replies with FEND-framed status

# Frame the detect command in KISS: FEND CMD_DETECT FEND
_RNODE_PROBE = bytes([_KISS_FEND, _KISS_CMD_DETECT, _KISS_FEND])


# ── Meshtastic protobuf probe ────────────────────────────────────────
# Meshtastic uses a 4-byte header: 0x94 0xC3 LEN_H LEN_L followed by
# a protobuf-encoded ToRadio/FromRadio. A 0-length ToRadio (just the
# 4-byte header) is harmless and at minimum causes the device to send
# back a configuration packet (or at least an ACK-shaped frame).
_MESHTASTIC_HEADER = bytes([0x94, 0xC3])
_MESHTASTIC_PROBE  = bytes([0x94, 0xC3, 0x00, 0x00])


# ── MeshCore probe ───────────────────────────────────────────────────
# MeshCore companion firmware speaks a text-or-CBOR line protocol over
# serial. The exact command set varies by build, but most MC firmwares
# respond to a bare newline by emitting a prompt, version banner, or
# JSON status blob. We send a couple of safe queries and accept any
# printable response that mentions "mesh" / "core" / "node" / "addr"
# / "version" (case-insensitive) as a MeshCore signal.
_MESHCORE_PROBE = b"\n?\n"


def _list_ports(explicit: list[str] | None) -> list[str]:
    """Resolve port list -- explicit override, else glob /dev/tty[USB|ACM]*."""
    if explicit:
        return [p for p in explicit if os.path.exists(p)]
    ports = sorted(
        set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    )
    return ports


def _port_is_busy(port: str) -> bool:
    """Best-effort check that another process holds the port.

    We don't want to fight rnsd / meshpoint for a port they're
    actively using -- closing/reopening would disrupt traffic.
    Try a non-blocking exclusive open; if it raises, port is busy.
    """
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return True
    try:
        # On Linux, attempting to set exclusive mode on an
        # already-opened port via TIOCEXCL raises EBUSY. Cheap.
        import fcntl, termios
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
            # Released as soon as we close.
            return False
        except OSError:
            return True
    finally:
        try: os.close(fd)
        except OSError: pass


def _read_for(ser: serial.Serial, duration_sec: float) -> bytes:
    """Read everything available within duration_sec. Coalesces chunks
    so we don't return prematurely after the first byte arrives."""
    deadline = time.time() + duration_sec
    chunks: list[bytes] = []
    while time.time() < deadline:
        n = ser.in_waiting
        if n:
            chunks.append(ser.read(n))
        else:
            time.sleep(0.02)
    return b"".join(chunks)


def _probe_rnode(ser: serial.Serial, timeout: float) -> bool:
    """Return True if reply pattern matches RNode KISS framing."""
    ser.reset_input_buffer()
    ser.write(_RNODE_PROBE)
    ser.flush()
    reply = _read_for(ser, timeout)
    # Match: starts with FEND and contains at least one valid KISS frame
    # (two FENDs). Be lenient -- some RNode firmwares prepend an
    # unframed boot banner before the first KISS reply.
    if not reply:
        return False
    return reply.count(bytes([_KISS_FEND])) >= 2 and _KISS_FEND in reply[:64]


def _probe_meshtastic(ser: serial.Serial, timeout: float) -> bool:
    """Return True if a Meshtastic-style protobuf header is echoed."""
    ser.reset_input_buffer()
    ser.write(_MESHTASTIC_PROBE)
    ser.flush()
    reply = _read_for(ser, timeout)
    # Meshtastic devices respond with their own 0x94 0xC3 header on
    # any FromRadio frame. If we see those two bytes anywhere in the
    # reply (and we did NOT see KISS-style 0xC0 framing first), it's
    # almost certainly Meshtastic.
    return _MESHTASTIC_HEADER in reply


_MESHCORE_HINTS = (b"mesh", b"core", b"node", b"addr", b"version", b"hello", b"ready")


def _probe_meshcore(ser: serial.Serial, timeout: float) -> bool:
    """Return True if reply contains printable text matching MC hints."""
    ser.reset_input_buffer()
    ser.write(_MESHCORE_PROBE)
    ser.flush()
    reply = _read_for(ser, timeout)
    if not reply:
        return False
    lower = reply.lower()
    return any(hint in lower for hint in _MESHCORE_HINTS)


def probe_one(port: str, baud: int, timeout: float) -> tuple[Optional[str], Optional[str]]:
    """Probe a single port. Returns (classification, error).

    classification is one of "rnode", "meshcore", "meshtastic", "unknown".
    error is None on success or a short string describing what went
    wrong (port couldn't open, etc.); when error is set, classification
    is None.
    """
    try:
        # exclusive=True keeps us from racing another process if it
        # opens between our busy check and here.
        ser = serial.Serial(
            port, baudrate=baud, timeout=0,
            write_timeout=1.0, exclusive=True,
        )
    except (serial.SerialException, OSError) as exc:
        return (None, f"open failed: {exc}")

    try:
        # Brief settle so any boot chatter from a just-replugged device
        # has a chance to land in the input buffer before our probes.
        time.sleep(0.15)

        # Order matters: RNode first (KISS framing is the most
        # distinctive signature), then Meshtastic (protobuf header),
        # then MeshCore (text heuristic; weakest match, last resort).
        if _probe_rnode(ser, timeout):
            return ("rnode", None)
        if _probe_meshtastic(ser, timeout):
            return ("meshtastic", None)
        if _probe_meshcore(ser, timeout):
            return ("meshcore", None)
        return ("unknown", None)
    except Exception as exc:  # noqa: BLE001
        return (None, f"probe raised: {exc}")
    finally:
        try: ser.close()
        except Exception: pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Identify USB-serial radio devices")
    ap.add_argument(
        "--ports", nargs="*", default=None,
        help="Explicit port list (default: glob /dev/ttyUSB* and /dev/ttyACM*)",
    )
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=0.6,
                    help="Per-probe reply window (default: 0.6s)")
    args = ap.parse_args()

    ports = _list_ports(args.ports)
    result = {
        "scanned":    ports,
        "busy":       [],
        "rnode":      None,
        "meshcore":   None,
        "meshtastic": None,
        "unknown":    [],
        "errors":     {},
    }

    for port in ports:
        if _port_is_busy(port):
            result["busy"].append(port)
            continue
        kind, err = probe_one(port, args.baud, args.timeout)
        if err:
            result["errors"][port] = err
            continue
        if kind == "unknown":
            result["unknown"].append(port)
            continue
        # Slot assignment -- first match wins; subsequent matches of
        # the same kind go into unknown with an explanatory error.
        if result.get(kind) is None:
            result[kind] = port
        else:
            result["unknown"].append(port)
            result["errors"][port] = (
                f"duplicate {kind} device; first was {result[kind]}"
            )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
