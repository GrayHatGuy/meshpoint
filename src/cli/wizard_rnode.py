"""RNode USB setup steps for the Meshpoint wizard.

Parallel to wizard_meshcore.py but for RNode LoRa devices running
Reticulum firmware.  Offers configuration of the rnode_usb capture
source when USB serial ports are detected during setup.
"""

from __future__ import annotations

from src.cli.hardware_detect import HardwareReport


def maybe_add_rnode_usb(
    config: dict,
    report: HardwareReport,
    confirm_fn,
    choose_fn,
    already_claimed: set[str] | None = None,
) -> None:
    """Offer to enable RNode USB capture if unclaimed USB serial ports exist.

    ``already_claimed`` contains ports already assigned to MeshCore or
    another source so they are not offered again.
    """
    claimed = already_claimed or set()
    capture_port = config.get("capture", {}).get("serial_port")

    candidates = [
        p for p in report.rnode_usb_candidates
        if p != capture_port and p not in claimed
    ]

    if not candidates:
        return

    print()
    print("        USB serial port(s) detected that could be an RNode")
    print("        (Reticulum network LoRa device):")
    for port in candidates:
        print(f"          - {port}")
    print()
    print("        If you have an RNode plugged in via USB, Meshpoint can")
    print("        capture Reticulum packets from it automatically.")
    print()

    if not confirm_fn("Enable RNode USB capture?"):
        config.setdefault("capture", {}).setdefault(
            "rnode_usb", {}
        )["auto_detect"] = False
        print("        RNode USB disabled.")
        print()
        return

    if len(candidates) == 1:
        chosen_port = candidates[0]
    else:
        chosen_port = choose_fn("Select RNode USB port:", candidates)

    rnode_cfg = config.setdefault("capture", {}).setdefault("rnode_usb", {})
    rnode_cfg["serial_port"] = chosen_port
    rnode_cfg["auto_detect"] = True

    # Persist radio parameters from default.yaml — user can tune in local.yaml
    print(f"        RNode USB capture enabled on {chosen_port}")
    print()
    print("        Radio parameters default to:")
    freq_hz = rnode_cfg.get("frequency_hz", 914_875_000)
    bw_hz   = rnode_cfg.get("bandwidth_hz", 125_000)
    sf      = rnode_cfg.get("spreading_factor", 8)
    cr      = rnode_cfg.get("coding_rate", 5)
    print(f"          frequency:        {freq_hz / 1e6:.4f} MHz")
    print(f"          bandwidth:        {bw_hz / 1e3:.0f} kHz")
    print(f"          spreading factor: SF{sf}")
    print(f"          coding rate:      4/{cr}")
    print()
    print("        These must match your Reticulum network.")
    print("        To change them edit capture.rnode_usb in config/local.yaml")
    print("        and restart: sudo systemctl restart meshpoint")
    print()
