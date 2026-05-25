"""Minimal RNode KISS serial driver (capture-only).

Implements the host-side KISS framing protocol for RNode LoRa devices:
  https://github.com/markqvist/RNode_Firmware

Frame format: [FEND][CMD][escaped data...][FEND]
RSSI frames arrive immediately before CMD_DATA; SNR frames before RSSI.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Callable, Optional

import serial

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# KISS framing constants
# ---------------------------------------------------------------------------
FEND  = 0xC0  # frame delimiter
FESC  = 0xDB  # escape byte
TFEND = 0xDC  # escaped FEND
TFESC = 0xDD  # escaped FESC

# ---------------------------------------------------------------------------
# RNode command bytes
# Source: markqvist/RNode_Firmware/Framing.h
# ---------------------------------------------------------------------------
CMD_DATA        = 0x00
CMD_FREQUENCY   = 0x01
CMD_BANDWIDTH   = 0x02
CMD_TXPOWER     = 0x03
CMD_SF          = 0x04
CMD_CR          = 0x05
CMD_RADIO_STATE = 0x06
CMD_DETECT      = 0x08
CMD_PROMISC     = 0x11   # NOTE: master-branch firmware defines this as 0x0E,
                         # but 0x0E breaks RX on the firmware version commonly
                         # flashed on existing RNode dongles. 0x11 is what the
                         # field-deployed firmware actually accepts. If the
                         # dongle isn't entering promisc mode, verify your
                         # firmware build with rnodeconf and update this byte.
CMD_STAT_RSSI   = 0x23
CMD_STAT_SNR    = 0x24
CMD_BOARD_INFO  = 0x47

# NOTE on sync word:
#   The RNode dongle's onboard ESP32 + radio (SX1276/SX1262) do hardware
#   sync word filtering before any byte reaches us over USB serial. By
#   the time KISS frames arrive on the host, they are already known-good
#   Reticulum frames matching the firmware-configured sync word.
#
#   The host therefore CANNOT change the sync word at runtime -- the
#   standard RNode firmware exposes no KISS command for it (verified
#   against markqvist/RNode_Firmware/Framing.h). To change the sync word
#   on the dongle, flash matching firmware via rnodeconf.
#
#   The ``expected_sync_word`` parameter to ``configure()`` is purely
#   informational: we log it so operators can confirm the network value,
#   and the same config field will drive the SX1302 per-channel sync
#   filter once the Step 2 HAL patch lands.

RADIO_STATE_ON  = 0x01
RADIO_STATE_OFF = 0x00

# RNode encodes RSSI as (dBm + offset); offset depends on chip family:
#   SX127x HF port (>525 MHz, e.g. 915 MHz): offset = 157
#   SX126x (newer boards):                   offset = 292
# SNR encoded as signed byte * 0.25 dB
_RSSI_OFFSET = 157   # SX127x HF port (Heltec, T-Beam, RAK, etc.)
_SNR_SCALE   = 4
_SNR_OFFSET  = 128

_SETTLE_SECONDS = 2.0


class RNodeDriver:
    """Serial driver for an RNode LoRa device (receive / capture only).

    Opens the serial port, configures radio parameters, and runs a daemon
    thread that parses incoming KISS frames. Received LoRa packets are
    delivered via the ``on_data`` callback, which is invoked from the
    reader thread — callers must be thread-safe (e.g. use
    ``loop.call_soon_threadsafe``).
    """

    def __init__(
        self,
        port: str,
        baud_rate: int = 115200,
        on_data: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.port      = port
        self.baud_rate = baud_rate
        self._on_data  = on_data

        # Updated by CMD_STAT_RSSI / CMD_STAT_SNR frames that precede each
        # CMD_DATA frame.  Read by the caller immediately after on_data fires.
        self.r_rssi: float = -120.0
        self.r_snr:  float = 0.0

        self._serial:  Optional[serial.Serial]  = None
        self._thread:  Optional[threading.Thread] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the serial port and start the background read loop."""
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baud_rate,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=0,          # non-blocking read
            xonxoff=False,
            rtscts=False,
        )
        time.sleep(_SETTLE_SECONDS)
        self._running = True
        self._thread  = threading.Thread(
            target=self._read_loop, daemon=True, name="rnode-read"
        )
        self._thread.start()
        logger.info("RNode driver opened %s @ %d baud", self.port, self.baud_rate)

    def close(self) -> None:
        """Stop the read loop and close the serial port."""
        self._running = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        logger.info("RNode driver closed")

    def configure(
        self,
        frequency_hz:    int,
        bandwidth_hz:    int,
        spreading_factor: int,
        coding_rate:     int,
        tx_power:        int,
        expected_sync_word: int = 0x42,
    ) -> None:
        """Send radio configuration and enable the receiver.

        Must be called after ``open()``.  The radio will not forward
        packets until all parameters have been accepted and the radio
        state is set to ON.

        ``expected_sync_word`` is informational on the USB path: the
        RNode dongle filters at the radio before the host sees frames,
        so this value just documents what the operator believes the
        network is using. It cannot be enforced via KISS -- use
        ``rnodeconf`` to change the dongle's actual sync word. If
        capture shows zero traffic, the firmware-set sync word does
        not match the network sync word.
        """
        self._send_cmd(CMD_FREQUENCY,   struct.pack(">I", frequency_hz))
        self._send_cmd(CMD_BANDWIDTH,   struct.pack(">I", bandwidth_hz))
        self._send_cmd(CMD_SF,          bytes([spreading_factor]))
        self._send_cmd(CMD_CR,          bytes([coding_rate]))
        self._send_cmd(CMD_TXPOWER,     bytes([tx_power]))
        self._send_cmd(CMD_RADIO_STATE, bytes([RADIO_STATE_ON]))
        self._send_cmd(CMD_PROMISC,     bytes([0x01]))
        logger.info(
            "RNode configured: %d Hz  BW=%d Hz  SF=%d  CR=4/%d  "
            "TXP=%d dBm  promisc=on  sync_word=0x%02X "
            "(filtered by dongle firmware; informational here)",
            frequency_hz, bandwidth_hz, spreading_factor, coding_rate,
            tx_power, expected_sync_word,
        )

    # ------------------------------------------------------------------
    # Internal: KISS write
    # ------------------------------------------------------------------

    def _send_cmd(self, cmd: int, data: bytes = b"") -> None:
        if not self._serial or not self._serial.is_open:
            return
        self._serial.write(self._encode(cmd, data))

    @staticmethod
    def _encode(cmd: int, data: bytes) -> bytes:
        escaped = bytearray()
        for byte in data:
            if byte == FEND:
                escaped += bytes([FESC, TFEND])
            elif byte == FESC:
                escaped += bytes([FESC, TFESC])
            else:
                escaped.append(byte)
        return bytes([FEND, cmd]) + bytes(escaped) + bytes([FEND])

    # ------------------------------------------------------------------
    # Internal: KISS read loop (daemon thread)
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        in_frame    = False
        cmd_read    = False
        escaped     = False
        command     = 0x00
        buf: bytearray = bytearray()

        while self._running:
            try:
                if not self._serial or not self._serial.is_open:
                    break

                chunk = self._serial.read(256)
                if not chunk:
                    time.sleep(0.08)
                    continue

                for byte in chunk:
                    if byte == FEND:
                        if in_frame and cmd_read:
                            self._handle_frame(command, bytes(buf))
                        # reset for next frame
                        in_frame = True
                        cmd_read = False
                        escaped  = False
                        command  = 0x00
                        buf      = bytearray()

                    elif in_frame:
                        if not cmd_read:
                            # first byte inside frame is always the command
                            command  = byte
                            cmd_read = True
                        elif escaped:
                            escaped = False
                            if byte == TFEND:
                                buf.append(FEND)
                            elif byte == TFESC:
                                buf.append(FESC)
                            else:
                                buf.append(byte)  # malformed escape, pass through
                        elif byte == FESC:
                            escaped = True
                        else:
                            buf.append(byte)

            except serial.SerialException as exc:
                logger.warning("RNode serial read error: %s", exc)
                break
            except Exception:
                logger.exception("RNode read loop unexpected error")
                break

    def _handle_frame(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_DATA:
            if self._on_data and data:
                try:
                    self._on_data(data)
                except Exception:
                    logger.exception("RNode on_data callback raised")

        elif cmd == CMD_STAT_RSSI:
            if data:
                self.r_rssi = float(data[0]) - _RSSI_OFFSET

        elif cmd == CMD_STAT_SNR:
            if data:
                raw = data[0] if data[0] < 128 else data[0] - 256  # treat as signed
                self.r_snr = raw / float(_SNR_SCALE)

        elif cmd == CMD_BOARD_INFO:
            logger.debug("RNode board info: %s", data.hex() if data else "(empty)")
