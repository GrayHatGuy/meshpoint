# Cold-start install: Meshpoint + Reticulum + MeshCore on a fresh Pi

End-to-end playbook for bringing up a Meshpoint node with all three
LoRa stacks (Meshtastic via SX1302 concentrator, Reticulum via USB
RNode, MeshCore via USB Heltec companion) on a Pi that has nothing
configured yet. Validated on a Raspberry Pi 4 + RAK2287 concentrator
HAT + 2× CP2102-based Heltec USB dongles.

If you're restoring an existing setup rather than starting blank,
skip to **Section 7 — Restore from backup** instead.

---

## 0. Hardware checklist

Before flashing anything, confirm you have:

- Raspberry Pi 4 (2 GB RAM minimum, 4 GB recommended)
- microSD card, 16 GB+ (32 GB if you want headroom for message history)
- RAK2287 SPI concentrator HAT *or* an equivalent SX1302-based concentrator (for Meshtastic)
- One USB LoRa dongle flashed with **RNode firmware** (for Reticulum / LXMF)
- One USB LoRa dongle flashed with **MeshCore companion firmware** (for MeshCore)
- Both USB dongles use the same SX126x family + CP2102 USB-UART → indistinguishable by USB metadata, which is why we ship `scripts/identify_radios.py`
- Optional: a second Pi or phone with MeshCore / Meshtastic for over-the-air testing

> **Heads-up on Heltec board firmware.** A Heltec V2 / V3 shipped from
> the factory typically has Meshtastic firmware on it. To use one as
> an RNode you need to flash **RNode firmware**; to use one as a
> MeshCore companion you need **MeshCore companion firmware**. The
> probe in `scripts/identify_radios.py` distinguishes them after the
> fact, but YOU have to put the right firmware on each board before
> plugging them in.

---

## 1. Flash Raspberry Pi OS

1. Use **Raspberry Pi Imager** with Pi OS Bookworm 64-bit (Lite is fine).
2. In the imager's advanced options:
    - Set hostname (`mprns` here; pick whatever)
    - Enable SSH with password OR key
    - Set the user `mp` (the rest of this doc assumes that username — if you change it, also change `MESHPOINT_RNSD_USER` env var in the meshpoint service later)
    - Configure Wi-Fi if you don't have ethernet
3. Boot the Pi, SSH in.

---

## 2. Base Meshpoint install

```bash
# Per the upstream Meshpoint install (see docs/ONBOARDING.md)
sudo apt update && sudo apt install -y git
sudo mkdir -p /opt/meshpoint
sudo chown $USER:$USER /opt/meshpoint
git clone https://github.com/GrayHatGuy/meshpoint-rnode.git /opt/meshpoint
cd /opt/meshpoint
git checkout rnsdusb           # the branch with all this Phase 1 + 2 work
sudo bash scripts/install.sh   # standard meshpoint install (concentrator, etc.)
```

Confirm Meshpoint is up on the web UI: `http://<pi-ip>:8080`. Dashboard
should populate within ~30 seconds. The Stats tab should show packets
flowing from the concentrator.

---

## 3. Plug in the LoRa USB dongles

Plug **both** USB dongles into the Pi's USB ports. Order doesn't
matter — the probe in step 4 will identify them functionally.

After plug-in, both should appear as serial nodes:

```bash
ls /dev/ttyUSB*
# /dev/ttyUSB0  /dev/ttyUSB1
```

(They might be `/dev/ttyACM*` on some boards. The probe handles both.)

---

## 4. Run setup_rnsd.sh — installs RNS stack, probes radios, wires configs

This is where the cold-start magic happens. `setup_rnsd.sh` will:

1. `pip install --user --break-system-packages rns lxmf inotify_simple meshcore` as the `mp` user
2. Add `mp` to the `dialout` group (USB serial access)
3. **Run `scripts/identify_radios.py`** — functionally probe both ports and decide which is the RNode vs which is the MeshCore. Output is JSON like:
   ```json
   {
     "rnode":    "/dev/ttyUSB0",
     "meshcore": "/dev/ttyUSB1"
   }
   ```
4. Drop Reticulum + LXMD config templates, **rewriting the `port = ...` line in `~mp/.reticulum/config` to match the probed RNode path**
5. Install systemd units for `rnsd`, `lxmd`, `lxmf-inbox-dump`
6. Install the `meshpoint-lxmf` sudoers grant
7. **Edit `/opt/meshpoint/config/local.yaml`**:
    - `rnode_usb.auto_detect: false`, `serial_port: <probed RNode>` (rnsd owns it; this pin feeds Meshpoint's existing exclusion list so the MC auto-detect skips that port)
    - `meshcore_usb.auto_detect: true`, `serial_port: <probed MeshCore>` (Meshpoint binds the right device on first try)
8. Grant `meshpoint` user read access to `mp`'s rnsd/lxmd artifacts (so the `/api/reticulum/*` endpoints work without sudo for every call)
9. `systemctl enable --now rnsd lxmd lxmf-inbox-dump`

Run it:

```bash
sudo bash /opt/meshpoint/scripts/setup_rnsd.sh
```

Look for these lines in the output:

```
[setup_rnsd]   RNode identified at: /dev/ttyUSB0
[setup_rnsd]   MeshCore identified at: /dev/ttyUSB1
[setup_rnsd] Reticulum config port already matches probed RNode (/dev/ttyUSB0)
[setup_rnsd] Pinning USB radio ports in local.yaml using probed assignments
[setup_rnsd] Installing /etc/sudoers.d/meshpoint-lxmf
[setup_rnsd] Enabling and starting rnsd.service
[setup_rnsd] Enabling and starting lxmd.service
[setup_rnsd] Enabling and starting lxmf-inbox-dump.service
```

If the probe lines don't show your devices, see **Section 6 — Troubleshooting**.

---

## 5. Verify end-to-end

### 5.1 — All four services are active

```bash
for svc in meshpoint rnsd lxmd lxmf-inbox-dump; do
    printf '%-22s %s\n' "$svc" "$(systemctl is-active "$svc")"
done
```

Every line should read `active`.

### 5.2 — Both USB radios bound to the right owner

```bash
sudo lsof /dev/ttyUSB0 /dev/ttyUSB1
```

Expected: `rnsd` owns the RNode port, `python` (meshpoint) owns the MeshCore port.

### 5.3 — Reticulum identity is broadcasting

```bash
curl -s http://localhost:8080/api/reticulum/identity | python3 -m json.tool
```

Should return `{"address": "<32 hex chars>", "display_name": "Meshpoint", ...}`.
That's your LXMF address — paste it into MeshChat / Sideband on another device to chat.

### 5.4 — MeshCore source is reading the dongle

```bash
sudo journalctl -u meshpoint --no-pager -n 20 | grep -i meshcore
```

Should include `meshcore_usb_source: ... started` (or similar).

### 5.5 — Send from another node, see it on the dashboard

Open `http://<pi-ip>:8080` → **Messages** tab → filter chips show
`All / MT / MC / RNS`. Send a MeshCore broadcast from your other
MC device; within a couple of seconds a `Public` conversation
appears under CHANNELS with a green `MC` badge. Same flow for
Meshtastic and Reticulum.

---

## 6. Troubleshooting cold-start

### Probe reports both ports as `"unknown"`

The RNode probe is definitive (KISS framing is unambiguous). If
RNode comes back null, the dongle may be wedged — physically
unplug + replug, run the probe again:

```bash
sudo systemctl stop meshpoint rnsd
sudo -u mp /opt/meshpoint/scripts/identify_radios.py --debug
```

The `--debug` output shows raw bytes sent / received per probe. If
the RNode replies with garbage instead of `c00846c0`, the RNode
firmware may be corrupted; reflash with `rnodeconf -p /dev/ttyUSBX`.

### MeshCore comes back `"unknown"`

Two fallbacks:
- The `--assume-mc-leftovers` flag infers MC by elimination when
  there's exactly one RNode + one unknown port. `setup_rnsd.sh`
  passes this flag by default, so cold-starts with the 2-device
  topology Just Work even if the `meshcore` Python lib failed to
  install.
- If `meshcore` lib installed cleanly, the binary probe is definitive
  — re-check the pip install succeeded:
  ```bash
  sudo -u mp pip show meshcore 2>/dev/null | head -3
  ```

### `Bad file descriptor` loop from rnsd

The RNode MCU is wedged behind the CP2102 chip — the USB layer is
fine but the firmware is in a stuck state. **Physically unplug the
RNode dongle, wait 3 seconds, plug it back in.** rnsd's reconnect
loop will pick it up.

### After unplugging/replugging the MeshCore device, MC traffic stops

Known issue: Meshpoint's MC source holds the old file descriptor and
doesn't notice the device's tty node was recreated. Workaround:

```bash
sudo systemctl restart meshpoint
```

Auto-reconnect is on the followup backlog.

---

## 7. Restore from backup (instead of fresh setup)

If you have a `meshpoint-backup-*.tar.gz` from a working Pi:

```bash
# Base Pi OS + Meshpoint base install (Sections 1-2 above)
# THEN:
sudo bash /opt/meshpoint/scripts/restore_meshpoint.sh \
    /path/to/meshpoint-backup-<hostname>-<timestamp>.tar.gz
```

Restore takes ~10 seconds. It stops all four services, extracts the
tarball straight to `/`, fixes ownership on `~mp/.reticulum` and
`~mp/.lxmd`, then restarts everything. Verify with the **Section 5**
checks.

To verify a backup's integrity *without* applying it:

```bash
sudo bash /opt/meshpoint/scripts/restore_meshpoint.sh --verify \
    /path/to/meshpoint-backup-<hostname>-<timestamp>.tar.gz
```

This unpacks just the metadata blobs and checks the SHA-256 manifest.
No live files are touched.

---

## 8. What `setup_rnsd.sh` writes (for future debugging)

| File | Purpose |
|---|---|
| `~mp/.reticulum/config` | rnsd interface config (the `port = ...` line is the probed RNode path) |
| `~mp/.lxmd/config` | lxmd LXMF config (display name, propagation node settings) |
| `/etc/systemd/system/rnsd.service` | rnsd unit (User=mp, ExecStart=`~mp/.local/bin/rnsd`) |
| `/etc/systemd/system/lxmd.service` | lxmd unit (PartOf=rnsd.service so cascades) |
| `/etc/systemd/system/lxmf-inbox-dump.service` | Our sidecar — decodes the messagestore + classifies peers |
| `/etc/sudoers.d/meshpoint-lxmf` | Narrow grant: meshpoint can run `lxmf_send.py`, `lxmf_announce.py`, `rnstatus`, plus `systemctl restart rnsd.service` |
| `/opt/meshpoint/config/local.yaml` | `capture.rnode_usb.serial_port` + `capture.meshcore_usb.serial_port` pinned from probe |
| `/opt/meshpoint/data/lxmf_sent.jsonl` | Append-only log of LXMF messages sent FROM this Meshpoint |
| `/opt/meshpoint/data/lxmf_contacts.json` | Operator-edited address book (override for classifier names) |
| `/opt/meshpoint/data/lxmf_announce.json` | Auto-announce period preference + last-fire metadata |

Everything operator-mutable lives under `/opt/meshpoint/data/` or
`~mp/`, so a `git pull` of the repo never destroys user state.
