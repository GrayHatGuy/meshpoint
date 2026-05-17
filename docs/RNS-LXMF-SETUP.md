# Reticulum + LXMF on a Meshpoint Pi

This page describes how to add real Reticulum messaging (LXMF) to a
Meshpoint Pi using the `rnsdusb` branch's "B1" architecture: the
**standard Reticulum stack** (`rns` + `lxmf` via pip) runs as its own
pair of systemd services and owns the USB RNode dongle, while
Meshpoint continues capturing Meshtastic (and optionally Reticulum
RX) on the SX1302 concentrator.

The two stacks coexist on the same Pi but never share the radio.

## Why B1 vs other approaches

| Approach | Hardware | Concentrator changes | New custom code | Effort |
|---|---|---|---|---|
| **B1 (this doc)** | concentrator + USB RNode | none | none | ~1 day |
| D-embedded | concentrator only | new channel plan + per-packet TX sync | custom `RNS.Interface` subclass | ~5 days |
| D-standalone | concentrator only | same as D-embedded | custom interface as RNS plugin | ~6 days |

B1 trades hardware ($30 USB RNode) for software simplicity. The custom
`ConcentratorInterface` work in approaches D would be reusable later
if you want to consolidate to a single radio -- B1 is the natural
starting point because RNS+LXMF on a USB RNode is the battle-tested
canonical setup and works on day one.

## Installation

```bash
cd /opt/meshpoint
sudo git pull        # make sure you're on the rnsdusb branch
sudo bash scripts/setup_rnsd.sh
```

The script is idempotent. It will:

1. `pip install --user rns lxmf` for your login user
2. Add you to the `dialout` group (lets `rnsd` open `/dev/ttyUSB*`
   without sudo)
3. Drop default configs into `~/.reticulum/config` and `~/.lxmd/config`
   **only if those files don't already exist**
4. Install `rnsd.service` and `lxmd.service` into systemd with your
   username and pip binary path substituted in
5. Set `capture.rnode_usb.auto_detect: false` and
   `capture.meshcore_usb.auto_detect: false` in Meshpoint's
   `local.yaml` so Meshpoint doesn't fight `rnsd` over the USB port
6. Enable and start both services
7. Print your LXMF address

## After install

### Your LXMF address

`rnsd` has a *transport identity* (the node-level RNS keypair).
**`lxmd` has its OWN identity** (the LXMF delivery destination). Your
LXMF address -- the one you give to peers -- is the lxmd address, not
the transport identity. Find it with:

```bash
journalctl -u lxmd --no-pager | grep "ready to receive" | tail -1
```

You'll see:
```
LXMF Router ready to receive on <16-char-hex>
```

That hex string is your address. Examples in this doc use
`a25d4a84af70a9dd65ac7061cee24819` as a stand-in.

### Verify the stack is healthy

```bash
sudo systemctl is-active rnsd lxmd
# expect: active / active

rnstatus
# expect: rnsd Up, RNodeInterface Up with Mode: Full, Airtime > 0 after traffic
```

### Send yourself a test message from MeshChat

On any Pi already running MeshChat that's on the same Reticulum LoRa
network (same frequency / SF / BW / sync word):

1. Open MeshChat in a browser
2. Wait for the new Meshpoint's announce to appear in the Network /
   Announces view (typically within 60 seconds of `lxmd` startup --
   `announce_at_start = yes` triggers an announce on every restart)
3. Click the announce, send "hello"
4. On the Meshpoint, run:
   ```bash
   journalctl -u lxmd --no-pager | grep -i "message\|received" | tail
   ```
   The inbound message will appear in `~/.lxmd/storage/messages/`.

## Operations

### Restart the stack

```bash
sudo systemctl restart rnsd
# lxmd auto-restarts with rnsd because of PartOf=rnsd.service in its unit file
```

### Watch live logs

```bash
journalctl -u rnsd -u lxmd -f
```

### Change radio parameters

Edit `~/.reticulum/config`, modify the `[[Meshpoint RNode USB]]`
block, then `sudo systemctl restart rnsd`. Common edits:

- **Different USB port**: change `port = /dev/ttyUSB0`. USB serial
  port numbers are not stable across reboots when multiple USB serial
  devices are plugged in -- if `rnstatus` shows the interface `Down`
  after a reboot, check `ls /dev/ttyUSB*`.
- **Different frequency / SF / BW**: must match the rest of your
  Reticulum LoRa network exactly.
- **Different TX power**: `txpower = 22` is conservative. The RNode
  firmware caps this at 22 dBm; higher values are silently clamped.

### Change your display name

Edit `~/.lxmd/config`, `[lxmf]` section, set `display_name = ...`.
This name is what peers see in their MeshChat / Sideband contact list.

```bash
sudo systemctl restart lxmd
```

## Troubleshooting

### `rnstatus` shows interface `Down`

```bash
ls /dev/ttyUSB*           # which device numbers actually exist?
sudo lsof /dev/ttyUSB0    # who is holding the port?
```

If a `python` process owned by `meshpoint` is holding the port, the
Meshpoint USB auto-detect snuck back on. Re-run the auto-detect-off
section of `setup_rnsd.sh` or set:

```yaml
capture:
  rnode_usb:
    auto_detect: false
  meshcore_usb:
    auto_detect: false
```

### `lxmd` crash-loops

```bash
sudo systemctl stop lxmd
~/.local/bin/lxmd        # run manually to see the real Python error
```

Most common cause: duplicate sections in `~/.lxmd/config` (especially
duplicate `[propagation]`). Edit the file, leave only one of each
section.

### MeshChat sees the announce but messages get stuck on "outbound"

Your announce hash and your lxmd-listening hash don't match.
**Make sure peers are sending to the address from
`journalctl -u lxmd | grep "ready to receive"`, NOT the address derived
from `~/.reticulum/storage/transport_identity`.**

### `rnsd` log spam about "AutoInterface: No multicast echoes"

This happens when there's another Reticulum LAN node nearby with a
different shared-instance secret. Either disable the AutoInterface
in `~/.reticulum/config` (the template ships with it omitted) or
ignore the noise.

### Announce never reaches LoRa (Airtime stays at 0)

Check `~/.reticulum/config`:

- `enable_transport = Yes` in `[reticulum]` (not `true` lowercase)
- `mode = full` inside `[[Meshpoint RNode USB]]`

If both are set and `Airtime` still doesn't move after an announce,
verify the RNode itself responds at all using `rnodeconf
/dev/ttyUSB0 -i`. The dongle's onboard firmware does its own LoRa
sync word filtering -- if you can't change the network sync word,
you'll need to reflash the dongle with `rnodeconf
/dev/ttyUSB0 --autoinstall`.

## Removing the stack

```bash
sudo systemctl disable --now lxmd.service rnsd.service
sudo rm /etc/systemd/system/rnsd.service /etc/systemd/system/lxmd.service
sudo systemctl daemon-reload
pip uninstall --user rns lxmf
```

The identity files (`~/.reticulum/storage/transport_identity` and the
LXMF identity in `~/.lxmd/storage/`) are NOT removed -- if you
reinstall later, your LXMF address stays the same.

## What's next

The B1 architecture leaves a clean migration path to D-standalone
(custom `ConcentratorInterface` plugin into the same `rnsd` you just
installed) for whoever decides to consolidate to a single radio
later. None of the work in this doc gets thrown away in that
migration.

The Meshpoint UI integration -- showing the live RNS identity, peer
list, and inbox in the existing Reticulum panels on the Radio tab --
is tracked separately under "Phase 2" on the `rnsdusb` branch.
