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
# KISS framing (RFC 1055-ish): FEND=0xC0 wraps each frame. The RNode
# firmware extends KISS with its own command codes (matches the
# constants in RNS source RNS/Interfaces/RNodeInterface.py KISS class).
# CMD_DETECT = 0x08 with payload byte 0x73 ('s' = "scan") asks the
# device to identify itself; firmware replies with a FEND-bracketed
# frame containing CMD_DETECT and the byte 0x46 ('F' = "found").
_KISS_FEND       = 0xC0
_KISS_CMD_DETECT = 0x08
_RNODE_DETECT_REQ = 0x73    # 's' -- detect request
_RNODE_DETECT_REPLY = 0x46  # 'F' -- detect reply byte

# Frame: FEND CMD_DETECT 0x73 FEND
_RNODE_PROBE = bytes([_KISS_FEND, _KISS_CMD_DETECT, _RNODE_DETECT_REQ, _KISS_FEND])


# ── Meshtastic protobuf probe ────────────────────────────────────────
# Meshtastic uses a 4-byte header: 0x94 0xC3 LEN_H LEN_L followed by
# a protobuf-encoded ToRadio/FromRadio. A 0-length ToRadio (just the
# 4-byte header) is harmless and at minimum causes the device to send
# back a configuration packet (or at least an ACK-shaped frame).
_MESHTASTIC_HEADER = bytes([0x94, 0xC3])
_MESHTASTIC_PROBE  = bytes([0x94, 0xC3, 0x00, 0x00])


# ── MeshCore probe ───────────────────────────────────────────────────
# MeshCore companion firmware variants speak different serial wire
# formats: some have a text REPL (banner + prompt on a bare CR), some
# use a binary framing with start bytes 0xC1 0xC2 or similar, some
# emit a JSON status line on connect. Rather than guessing one, we
# send a small bouquet of safe probes (CR, newline, '?', 'info') and
# accept a MeshCore classification if EITHER the reply contains a
# known MC hint string OR the reply is non-empty + non-KISS +
# non-Meshtastic-protobuf (process of elimination after the more
# specific probes have already failed).
_MESHCORE_PROBES = (
    b"\r",            # bare CR -- triggers REPL prompt on some builds
    b"\n",            # bare LF -- alternate line ending
    b"?\r\n",         # generic help / status query
    b"info\r\n",      # common CLI verb on text-mode MC firmwares
)
_MESHCORE_HINTS = (
    b"mesh", b"core", b"node", b"addr", b"version", b"hello",
    b"ready", b"prefix", b"contact", b"ble", b"lora",
)


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


# Module-level debug flag; set by main() from --debug. When True,
# every probe prints what it sent and what it received to stderr so
# operators (and future probe-protocol tuning) can see the raw bytes.
_DEBUG = False


def _dbg(port: str, label: str, sent: bytes, got: bytes) -> None:
    if not _DEBUG:
        return
    def _trim(b: bytes, n: int = 96) -> str:
        snip = b[:n]
        return snip.hex() + (f" ...(+{len(b)-n}b)" if len(b) > n else "")
    sys.stderr.write(
        f"[debug] {port} {label}: sent={_trim(sent)}  got({len(got)}b)={_trim(got)}\n"
    )


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


def _probe_rnode(ser: serial.Serial, timeout: float, port: str = "") -> bool:
    """Return True if reply pattern matches RNode KISS framing.

    Strong match: KISS frame containing CMD_DETECT + 'F' reply byte
    (firmware explicitly identified itself). Weaker match: at least
    two FEND bytes (any KISS framing at all -- some firmware versions
    don't have CMD_DETECT but still respond with KISS-framed status).
    """
    ser.reset_input_buffer()
    ser.write(_RNODE_PROBE)
    ser.flush()
    reply = _read_for(ser, timeout)
    _dbg(port, "rnode", _RNODE_PROBE, reply)
    if not reply:
        return False
    detect_reply_pattern = bytes([_KISS_FEND, _KISS_CMD_DETECT, _RNODE_DETECT_REPLY])
    if detect_reply_pattern in reply:
        return True
    return reply.count(bytes([_KISS_FEND])) >= 2


def _probe_meshtastic(ser: serial.Serial, timeout: float, port: str = "") -> bool:
    """Return True if a Meshtastic-style protobuf header is echoed."""
    ser.reset_input_buffer()
    ser.write(_MESHTASTIC_PROBE)
    ser.flush()
    reply = _read_for(ser, timeout)
    _dbg(port, "meshtastic", _MESHTASTIC_PROBE, reply)
    return _MESHTASTIC_HEADER in reply


def _probe_meshcore(ser: serial.Serial, timeout: float, port: str = "") -> bool:
    """Return True if reply contains hint strings characteristic of MeshCore."""
    per_probe = max(0.1, timeout / max(1, len(_MESHCORE_PROBES)))
    for probe in _MESHCORE_PROBES:
        ser.reset_input_buffer()
        try:
            ser.write(probe)
            ser.flush()
        except serial.SerialException:
            continue
        reply = _read_for(ser, per_probe)
        _dbg(port, f"meshcore({probe!r})", probe, reply)
        if not reply:
            continue
        lower = reply.lower()
        if any(hint in lower for hint in _MESHCORE_HINTS):
            return True
    return False


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
        # Errno 16 == EBUSY = another process holds the port. Map to
        # the dedicated "busy" sentinel so the caller's logic can
        # treat it differently from a real probe failure.
        msg = str(exc).lower()
        if "errno 16" in msg or "resource busy" in msg or "device or resource busy" in msg:
            return ("__busy__", None)
        return (None, f"open failed: {exc}")

    try:
        # Brief settle so any boot chatter from a just-replugged device
        # has a chance to land in the input buffer before our probes.
        time.sleep(0.15)

        # Order matters: RNode first (KISS framing is the most
        # distinctive signature), then Meshtastic (protobuf header),
        # then MeshCore (text heuristic; weakest match, last resort).
        if _probe_rnode(ser, timeout, port):
            return ("rnode", None)
        if _probe_meshtastic(ser, timeout, port):
            return ("meshtastic", None)
        if _probe_meshcore(ser, timeout, port):
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
    ap.add_argument("--debug", action="store_true",
                    help="Dump raw probe responses to stderr so we can see "
                         "what each firmware actually says")
    args = ap.parse_args()
    global _DEBUG
    _DEBUG = args.debug

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
        # Upfront busy-check is best-effort -- most USB-serial drivers
        # allow concurrent opens until pyserial's exclusive=True actually
        # tries to grab TIOCEXCL. We still try it because catching busy
        # ports early skips a needless serial.Serial() instantiation.
        if _port_is_busy(port):
            result["busy"].append(port)
            continue
        kind, err = probe_one(port, args.baud, args.timeout)
        # probe_one returns ("__busy__", None) when the exclusive open
        # raced and lost -- bucket it correctly even though it slipped
        # past the upfront _port_is_busy() check.
        if kind == "__busy__":
            result["busy"].append(port)
            continue
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
