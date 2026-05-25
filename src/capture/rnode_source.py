"""Capture source for RNode USB devices (Reticulum network, receive-only).

Connects to an RNode LoRa transceiver via USB serial using the KISS
protocol, configures the radio, and yields received frames as RawCapture
objects for the pipeline.

RNode.py uses a daemon thread for serial I/O; this class bridges that
thread into asyncio via a Queue and loop.call_soon_threadsafe.

Includes auto-detect, exponential-backoff reconnect, and periodic health
checks -- mirroring the pattern used by MeshcoreUsbCaptureSource.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

from src.capture.base import CaptureSource
from src.models.packet import Protocol, RawCapture
from src.models.signal import SignalMetrics

logger = logging.getLogger(__name__)

_EMPTY_SIGNAL = SignalMetrics(
    rssi=-120.0, snr=0.0, frequency_mhz=0.0,
    spreading_factor=0, bandwidth_khz=0.0, coding_rate="N/A",
)

_HEALTH_CHECK_INTERVAL_SECONDS   = 180
_HEALTH_CHECK_RETRY_DELAY_SECONDS = 20
_HEALTH_CHECK_MAX_FAILURES        = 2
_RECENT_EVENT_HEALTHY_WINDOW      = 120
_RECONNECT_BASE_DELAY_SECONDS     = 5
_RECONNECT_MAX_DELAY_SECONDS      = 60
_DTR_RESET_PULSE_SECONDS          = 0.1


class RnodeCaptureSource(CaptureSource):
    """Receives Reticulum frames from an RNode device connected via USB serial.

    Capture-only: the radio is placed in promiscuous mode so all
    over-the-air frames are forwarded to the host regardless of
    destination address.  No transmit path is wired.
    """

    def __init__(
        self,
        serial_port:      Optional[str] = None,
        baud_rate:        int = 115200,
        frequency_hz:     int = 914_875_000,
        bandwidth_hz:     int = 125_000,
        spreading_factor: int = 8,
        coding_rate:      int = 5,
        tx_power:         int = 22,
        sync_word:        int = 0x42,
        auto_detect:      bool = True,
        exclude_ports:    frozenset[str] = frozenset(),
    ) -> None:
        self._configured_port  = serial_port
        self._baud_rate        = baud_rate
        self._frequency_hz     = frequency_hz
        self._bandwidth_hz     = bandwidth_hz
        self._spreading_factor = spreading_factor
        self._coding_rate      = coding_rate
        self._tx_power         = tx_power
        self._sync_word        = sync_word
        self._auto_detect      = auto_detect
        self._exclude_ports    = exclude_ports

        self._driver = None          # RNodeDriver instance
        self._resolved_port: Optional[str] = None
        self._queue: asyncio.Queue  = asyncio.Queue(maxsize=500)
        self._loop:  Optional[asyncio.AbstractEventLoop] = None

        self._running  = False
        self._connected = False
        self._last_event_at: float = 0.0

        self._health_task:    Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # CaptureSource interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "rnode_usb"

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        port = await self._resolve_port()
        if port is None:
            logger.info(
                "No RNode USB device detected -- source idle "
                "(plug in an RNode and restart to activate)"
            )
            return

        self._resolved_port = port
        self._running = True
        await asyncio.to_thread(self._open_driver, port)

        if self._connected:
            self._health_task = asyncio.create_task(
                self._health_check_loop(), name="rnode-health"
            )
            return

        logger.info("RNode USB initial connect failed -- scheduling background reconnect")
        self._reconnect_task = asyncio.create_task(
            self._reconnect_until_connected(), name="rnode-initial-reconnect"
        )

    async def stop(self) -> None:
        self._running = False

        for task in (self._reconnect_task, self._health_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._reconnect_task = None
        self._health_task    = None
        await asyncio.to_thread(self._close_driver)
        logger.info("RNode USB source stopped")

    async def packets(self) -> AsyncIterator[RawCapture]:
        if not self._running:
            return

        while self._running:
            try:
                raw = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield raw
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Driver lifecycle (called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _open_driver(self, port: str) -> None:
        from src.hal.rnode_driver import RNodeDriver
        try:
            self._driver = RNodeDriver(
                port=port,
                baud_rate=self._baud_rate,
                on_data=self._on_frame,
            )
            self._driver.open()
            self._driver.configure(
                frequency_hz=self._frequency_hz,
                bandwidth_hz=self._bandwidth_hz,
                spreading_factor=self._spreading_factor,
                coding_rate=self._coding_rate,
                tx_power=self._tx_power,
                expected_sync_word=self._sync_word,
            )
            self._connected = True
            logger.info(
                "RNode USB source started on %s @ %d baud  "
                "(%d Hz  BW=%d  SF=%d  CR=4/%d  expected_sync=0x%02X)",
                port, self._baud_rate,
                self._frequency_hz, self._bandwidth_hz,
                self._spreading_factor, self._coding_rate,
                self._sync_word,
            )
        except Exception:
            logger.exception("Failed to open RNode USB on %s", port)
            self._connected = False
            self._driver    = None

    def _close_driver(self) -> None:
        self._connected = False
        if self._driver:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None

    # ------------------------------------------------------------------
    # Frame callback (called from RNode daemon thread)
    # ------------------------------------------------------------------

    def _on_frame(self, data: bytes) -> None:
        """Invoked by RNodeDriver's read thread on each received LoRa frame."""
        if not self._running or self._loop is None:
            return

        rssi = getattr(self._driver, "r_rssi", -120.0)
        snr  = getattr(self._driver, "r_snr",  0.0)
        raw  = self._build_raw_capture(data, rssi, snr)
        self._loop.call_soon_threadsafe(self._enqueue, raw)

    def _enqueue(self, raw: RawCapture) -> None:
        """Called on the event loop thread."""
        self._last_event_at = self._loop.time() if self._loop else 0.0
        try:
            self._queue.put_nowait(raw)
        except asyncio.QueueFull:
            logger.warning("RNode USB queue full, dropping frame")

    def _build_raw_capture(self, data: bytes, rssi: float, snr: float) -> RawCapture:
        return RawCapture(
            payload=data,
            signal=SignalMetrics(
                rssi=float(rssi),
                snr=float(snr),
                frequency_mhz=self._frequency_hz / 1_000_000.0,
                spreading_factor=self._spreading_factor,
                bandwidth_khz=self._bandwidth_hz / 1_000.0,
                coding_rate=f"4/{self._coding_rate}",
            ),
            capture_source="rnode_usb",
            protocol_hint=Protocol.RETICULUM,
        )

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    async def _reconnect_until_connected(self) -> None:
        try:
            await self._reconnect()
            if self._connected:
                self._health_task = asyncio.create_task(
                    self._health_check_loop(), name="rnode-health"
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("RNode USB initial reconnect loop error")

    async def _reconnect(self) -> None:
        await asyncio.to_thread(self._close_driver)

        delay   = _RECONNECT_BASE_DELAY_SECONDS
        attempt = 0

        while self._running:
            logger.info("RNode USB reconnecting in %ds...", delay)
            await asyncio.sleep(delay)

            if not self._running:
                return

            attempt += 1

            # Pulse DTR to hard-reset ESP32-based RNodes on retry
            if attempt >= 2 and self._resolved_port:
                await asyncio.to_thread(self._pulse_dtr, self._resolved_port)
                await asyncio.sleep(2.0)

            await asyncio.to_thread(self._open_driver, self._resolved_port)

            if self._connected:
                logger.info("RNode USB reconnected successfully")
                return

            delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)

    def _pulse_dtr(self, port: str) -> None:
        """Toggle DTR low briefly to reset an ESP32 RNode. Best-effort."""
        try:
            import serial as _serial
            import time as _time
            with _serial.Serial(port, self._baud_rate, timeout=0.5) as ser:
                ser.dtr = False
                _time.sleep(_DTR_RESET_PULSE_SECONDS)
                ser.dtr = True
            logger.info("RNode USB pulsed DTR on %s", port)
        except Exception as exc:
            logger.debug("RNode USB DTR pulse skipped on %s: %s", port, exc)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def _health_check_loop(self) -> None:
        consecutive_failures = 0
        try:
            while self._running and self._connected:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL_SECONDS)

                if not self._running:
                    return

                if self._has_recent_activity():
                    consecutive_failures = 0
                    continue

                if self._driver_is_alive():
                    consecutive_failures = 0
                    continue

                consecutive_failures += 1
                logger.info(
                    "RNode USB health probe missed (%d/%d)",
                    consecutive_failures, _HEALTH_CHECK_MAX_FAILURES,
                )

                if consecutive_failures < _HEALTH_CHECK_MAX_FAILURES:
                    await asyncio.sleep(_HEALTH_CHECK_RETRY_DELAY_SECONDS)
                    continue

                logger.warning(
                    "RNode USB health check failed %d times -- reconnecting",
                    _HEALTH_CHECK_MAX_FAILURES,
                )
                consecutive_failures = 0
                await self._reconnect()

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("RNode USB health check loop error")

    def _has_recent_activity(self) -> bool:
        if self._last_event_at == 0.0 or self._loop is None:
            return False
        return (self._loop.time() - self._last_event_at) < _RECENT_EVENT_HEALTHY_WINDOW

    def _driver_is_alive(self) -> bool:
        """Check that the RNode read thread is still running."""
        if not self._driver:
            return False
        thread = getattr(self._driver, "_thread", None)
        return thread is not None and thread.is_alive()

    # ------------------------------------------------------------------
    # Port resolution
    # ------------------------------------------------------------------

    async def _resolve_port(self) -> Optional[str]:
        if self._configured_port:
            return self._configured_port

        if not self._auto_detect:
            return None

        from src.capture.rnode_detect import detect_rnode_port
        return await detect_rnode_port(
            exclude_ports=self._exclude_ports, baud=self._baud_rate
        )
