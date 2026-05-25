"""Meshtastic Concentrator Gateway -- Edge Device Entry Point."""

from __future__ import annotations

import asyncio
import logging

from src.config import load_config, validate_activation
from src.coordinator import PipelineCoordinator
from src.log_format import print_banner, print_packet, setup_logging

setup_logging()
logger = logging.getLogger("concentrator")


def _add_serial_source(coordinator: PipelineCoordinator, config) -> None:
    try:
        from src.capture.serial_source import SerialCaptureSource
        coordinator.capture_coordinator.add_source(
            SerialCaptureSource(
                port=config.capture.serial_port,
                baud=config.capture.serial_baud,
            )
        )
    except ImportError:
        logger.warning(
            "Serial capture unavailable -- meshtastic package not installed"
        )


def _add_concentrator_source(coordinator: PipelineCoordinator, config) -> None:
    try:
        from src.capture.concentrator_source import ConcentratorCaptureSource
        coordinator.capture_coordinator.add_source(
            ConcentratorCaptureSource(
                spi_path=config.capture.concentrator_spi_device,
                syncword=config.radio.sync_word,
                radio_config=config.radio,
            )
        )
    except Exception:
        logger.exception("Concentrator source unavailable")


def _add_meshcore_usb_source(
    coordinator: PipelineCoordinator,
    config,
    exclude_ports: frozenset[str] = frozenset(),
) -> None:
    try:
        from src.capture.meshcore_usb_source import MeshcoreUsbCaptureSource
        usb_cfg = config.capture.meshcore_usb
        # If a port is explicitly configured, use it directly.
        # If auto-detecting, honour the exclusion list so we never try to
        # open a port already claimed by the RNode source.
        serial_port = usb_cfg.serial_port
        if serial_port and serial_port in exclude_ports:
            logger.warning(
                "MeshCore USB port %s is already claimed by another source "
                "-- skipping MeshCore USB",
                serial_port,
            )
            return
        coordinator.capture_coordinator.add_source(
            MeshcoreUsbCaptureSource(
                serial_port=serial_port,
                baud_rate=usb_cfg.baud_rate,
                auto_detect=usb_cfg.auto_detect,
                exclude_ports=exclude_ports,
            )
        )
    except ImportError:
        logger.warning(
            "MeshCore USB unavailable -- meshcore package not installed"
        )


def _add_rnode_usb_source(
    coordinator: PipelineCoordinator,
    config,
    exclude_ports: frozenset[str] = frozenset(),
) -> None:
    try:
        from src.capture.rnode_source import RnodeCaptureSource
        rnode_cfg = config.capture.rnode_usb
        serial_port = rnode_cfg.serial_port
        if serial_port and serial_port in exclude_ports:
            logger.warning(
                "RNode USB port %s is already claimed by another source "
                "-- skipping RNode USB",
                serial_port,
            )
            return
        coordinator.capture_coordinator.add_source(
            RnodeCaptureSource(
                serial_port=serial_port,
                baud_rate=rnode_cfg.baud_rate,
                frequency_hz=rnode_cfg.frequency_hz,
                bandwidth_hz=rnode_cfg.bandwidth_hz,
                spreading_factor=rnode_cfg.spreading_factor,
                coding_rate=rnode_cfg.coding_rate,
                tx_power=rnode_cfg.tx_power,
                sync_word=rnode_cfg.sync_word,
                auto_detect=rnode_cfg.auto_detect,
                exclude_ports=exclude_ports,
            )
        )
    except ImportError:
        logger.warning(
            "RNode USB unavailable -- pyserial package not installed"
        )


async def run_standalone() -> None:
    """Run the pipeline without the web dashboard (CLI mode)."""
    config = load_config()
    validate_activation(config)
    coordinator = PipelineCoordinator(config)

    # Build exclusion sets so RNode and MeshCore never contend for the same port.
    rnode_port    = config.capture.rnode_usb.serial_port
    meshcore_port = config.capture.meshcore_usb.serial_port
    rnode_excludes    = frozenset({meshcore_port} if meshcore_port else set())
    meshcore_excludes = frozenset({rnode_port}    if rnode_port    else set())

    for source_name in config.capture.sources:
        if source_name == "serial":
            _add_serial_source(coordinator, config)
        elif source_name == "concentrator":
            _add_concentrator_source(coordinator, config)
        elif source_name == "meshcore_usb":
            _add_meshcore_usb_source(coordinator, config, meshcore_excludes)
        elif source_name == "rnode_usb":
            _add_rnode_usb_source(coordinator, config, rnode_excludes)

    if (
        "meshcore_usb" not in config.capture.sources
        and config.capture.meshcore_usb.auto_detect
    ):
        _add_meshcore_usb_source(coordinator, config, meshcore_excludes)

    if (
        "rnode_usb" not in config.capture.sources
        and config.capture.rnode_usb.auto_detect
    ):
        _add_rnode_usb_source(coordinator, config, rnode_excludes)

    coordinator.on_packet(lambda pkt: print_packet(pkt))
    await coordinator.start()
    print_banner(config)
    logger.info("Standalone mode -- listening for packets")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await coordinator.stop()


if __name__ == "__main__":
    asyncio.run(run_standalone())
