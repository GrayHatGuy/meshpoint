"""MeshCore USB companion setup steps for the Meshpoint wizard."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from contextlib import contextmanager
from typing import Iterator

from src.cli.hardware_detect import HardwareReport

logger = logging.getLogger(__name__)

_SERVICE_NAME = "meshpoint"
_PORT_RELEASE_DELAY_SECONDS = 3
_REBOOT_VERIFY_DELAY_SECONDS = 8


def _is_systemd() -> bool:
    """Return True when systemd is available to manage the service."""
    return os.path.isdir("/run/systemd/system")


@contextmanager
def _release_serial_port() -> Iterator[bool]:
    """Stop the running meshpoint service so the wizard can open the port.

    Yields True if the service was stopped (or systemd is unavailable so the
    wizard can proceed anyway), False if the stop attempt failed. Restarts
    the service on exit so a wizard crash never leaves the device down.
    """
    if not _is_systemd():
        yield True
        return

    print("        Pausing meshpoint service to free the serial port...")
    stop = subprocess.run(
        ["sudo", "systemctl", "stop", _SERVICE_NAME],
        check=False,
        capture_output=True,
    )
    stopped = stop.returncode == 0
    if not stopped:
        print("        Could not stop meshpoint service (try 'sudo meshpoint setup').")

    if stopped:
        time.sleep(_PORT_RELEASE_DELAY_SECONDS)

    try:
        yield stopped
    finally:
        print("        Restarting meshpoint service...")
        subprocess.run(
            ["sudo", "systemctl", "restart", _SERVICE_NAME],
            check=False,
            capture_output=True,
        )


def maybe_add_meshcore_usb(
    config: dict,
    report: HardwareReport,
    confirm_fn,
    choose_fn,
    already_claimed: set[str] | None = None,
) -> None:
    """Offer to enable MeshCore USB monitoring if unclaimed USB serial ports exist.

    ``already_claimed`` contains ports already assigned to RNode or another
    source so they are not offered as MeshCore candidates.
    """
    claimed = already_claimed or set()
    capture_port = config.get("capture", {}).get("serial_port")
    candidates = [
        p for p in report.meshcore_usb_candidates
        if p != capture_port and p not in claimed
    ]

    if not candidates:
        return

    print()
    print("        USB serial port(s) detected that could be a MeshCore node:")
    for port in candidates:
        print(f"          - {port}")
    print()
    print("        If you have a MeshCore device (Heltec, T-Beam, etc.) plugged")
    print("        in via USB, Meshpoint can monitor its traffic automatically.")
    print()

    if not confirm_fn("Enable MeshCore USB monitoring?"):
        config.setdefault("capture", {}).setdefault(
            "meshcore_usb", {}
        )["auto_detect"] = False
        print("        MeshCore USB disabled.")
        print()
        return

    sources = config.setdefault("capture", {}).setdefault("sources", [])
    if "meshcore_usb" not in sources:
        sources.append("meshcore_usb")

    if len(candidates) == 1:
        chosen_port = candidates[0]
    else:
        chosen_port = choose_fn(
            "Select MeshCore USB port:", candidates
        )

    config["capture"].setdefault("meshcore_usb", {})["serial_port"] = (
        chosen_port
    )
    print(f"        MeshCore USB enabled on {chosen_port}")
    print()

    selected_region = config.get("radio", {}).get("region", "US")
    configure_meshcore_radio(chosen_port, selected_region, confirm_fn)


def configure_meshcore_radio(
    port: str,
    region: str = "US",
    confirm_fn=None,
    prompt_float_fn=None,
) -> None:
    """Configure the MeshCore companion's radio frequency.

    If the selected region has a known MeshCore preset (US, EU, ANZ),
    it is applied automatically. Otherwise the user is prompted for
    custom parameters or can skip.

    Stops the meshpoint service first so it does not contend for the
    serial port, then restarts it before returning.
    """
    from src.cli.meshcore_radio_config import (
        REGION_PRESETS,
        configure_radio,
        query_radio,
        verify_radio,
    )

    if confirm_fn is None:
        from src.cli.setup_wizard import _confirm as confirm_fn
    if prompt_float_fn is None:
        from src.cli.setup_wizard import _prompt_float as prompt_float_fn

    with _release_serial_port():
        print("        Querying companion radio settings...")
        status = query_radio(port)

        if not status:
            print("        Could not read current radio settings.")
            print("        The companion did not respond on the selected port.")
            print("        Skipping MeshCore radio configuration -- you can")
            print("        re-run 'meshpoint meshcore-radio' later.")
            print()
            return

        model_str = f" ({status.model})" if status.model else ""
        print(f"        Device: {status.name}{model_str}")
        print(f"        Current: {status.summary()}")
        print()

        meshcore_region_map = {"US": "US", "EU_868": "EU", "ANZ": "ANZ"}
        auto_preset_key = meshcore_region_map.get(region)

        if auto_preset_key and auto_preset_key in REGION_PRESETS:
            preset = REGION_PRESETS[auto_preset_key]
            print(f"        Applying {auto_preset_key} MeshCore preset")
            print(f"        ({preset.label})")
            if not confirm_fn("Apply this preset?", default_yes=True):
                print("        Skipped. Configure manually later if needed.")
                print()
                return
            freq = preset.frequency_mhz
            bw = preset.bandwidth_khz
            sf = preset.spreading_factor
            cr = preset.coding_rate
        else:
            print("        No standard MeshCore preset for your region.")
            print("        Enter custom radio parameters, or skip.")
            print()
            if not confirm_fn("Enter custom MeshCore radio settings?"):
                print("        Skipped.")
                print()
                return
            freq = prompt_float_fn("Frequency MHz (e.g. 910.525):")
            bw = prompt_float_fn("Bandwidth kHz (e.g. 62.5):")
            sf_val = prompt_float_fn("Spreading factor (e.g. 7):")
            cr_val = prompt_float_fn("Coding rate (e.g. 5):")
            if None in (freq, bw, sf_val, cr_val):
                print("        Invalid input. Skipping radio configuration.")
                print()
                return
            sf = int(sf_val)
            cr = int(cr_val)

        print(f"        Setting radio to {freq} MHz / BW{bw} / SF{sf} / CR{cr}...")

        ok = configure_radio(port, freq, bw, sf, cr)
        if not ok:
            print("        Failed to configure radio. Check the device and retry.")
            print()
            return

        print("        Radio configured. Companion is rebooting...")
        time.sleep(_REBOOT_VERIFY_DELAY_SECONDS)

        verified = verify_radio(port)
        if verified:
            print(f"        Verified: {verified.summary()}")
        else:
            print("        Could not verify (device may still be rebooting).")
            print("        The settings will apply on next power cycle.")

        print()
