# Investigation: MT RX regression on rnsdusb branch

**Status:** open
**First observed:** 2026-05-19 (soak testing between MP and MPRNS Meshpoints)
**Affects:** MT LongFast channel-broadcast receive on the rnsdusb-branch Meshpoint (MPRNS). Outbound TX from the same box is fine.

---

## Symptom

- **MP** (stock upstream v0.7.2, outdoor antenna): receives MT broadcasts from MPRNS at **>90%** delivery; sees **113 / 562** nodes total.
- **MPRNS** (rnsdusb branch, indoor antenna): receives MT broadcasts from MP at **~30%** delivery; sees **24 / 47** nodes total.
- Indoor antenna alone does NOT explain it — operator confirms the same MPRNS hardware captured outdoor MT traffic reliably **before** the rnsdusb modifications were applied.
- LXMF on MPRNS also drops packets and shows higher latency than MeshChat-to-MeshChat — likely a separate but coexisting issue on the same host (rnsd `mode = full` transport overhead, sidecar polling, CPU contention).
- MeshCore between MP and MPRNS is reliable both directions — MC uses its own USB Heltec, not the SX1302, so it's exonerated from this investigation.

## What we've already ruled out

- **Multi-protocol concentrator mode.** Same 30% delivery with `capture.concentrator_multi_protocol: false` in MPRNS's `local.yaml`. The journal confirms the simple plan is loaded: `Sync word set to 0x2B` (single value, not pair) and `Frequency 906.875 MHz / SF11 / BW250 (US)`.
- **RNS chatter on a shared IF chain.** RNS announces are at 914.875 MHz on a separate IF chain group from MT at 906.875 MHz. Even when multi-protocol was on, the two protocols don't share the same demod.
- **DB lock contention.** Fixed in `d38669c` (WAL + `synchronous=NORMAL` + `busy_timeout=5000`). Regression persists.
- **Capture queue saturation.** `grep -c "Capture queue full"` returns 0 over the affected window.
- **SX1302 RX hardware on MPRNS.** It hears non-MP traffic at healthy RSSI/SNR (e.g. -23 dBm / +9 dB from local RNS announces, -55 dBm / +6 dB from MP's MT packets that do arrive). When packets arrive, they're clean.
- **RSSI rescale.** `7b4f3c6` only changed display math in `rnode_decoder.py`, not radio gain registers.

## Leading suspects (HAL-touching commits on rnsdusb)

Listed in commit-graph order from upstream merge `9514daf` forward; bisect candidates that affect the single-protocol code path as well as multi-protocol:

| Commit | Subject | Why suspect |
|---|---|---|
| `b435445` | Drop NO_CRC and unknown-status packets at HAL boundary | Could be over-aggressively filtering valid MT frames |
| `41d1381` | Revert CMD_PROMISC to 0x11 (field-deployed firmware compatibility) | Promiscuous-mode register controls which frames the HAL forwards |
| `f9deaec` | Add RNS sync_word config + fix CMD_PROMISC bug + MT/MC plan tweak | Touches CMD_PROMISC again |
| `33ece45` | Step 2: per-demod-pair sync word + TX sync override (HAL + wiring) | Even the legacy single-sync path runs through the new wrapper |
| `c905e66` | Fix RSSI offset, pipeline resilience, and banner label | "Pipeline resilience" change could affect packet handling |
| `b16b0067` | (any other concentrator/HAL-touching commit found via `git log -- src/hal/ src/capture/`) | TBD |

Quick list:

```bash
git log --oneline 9514daf..HEAD -- src/hal/ src/capture/
```

## Bisect plan

**Goal:** find the first commit on rnsdusb where MT inbound delivery drops below 80% from a known-good MP sender.

### Setup (one-time)

```bash
# On MPRNS, create a worktree on the stock upstream merge point.
sudo git -c safe.directory=/opt/meshpoint -C /opt/meshpoint \
  worktree add /tmp/meshpoint-bisect 9514daf

# Optionally copy local.yaml so the bisect runs with the same config:
sudo cp /opt/meshpoint/config/local.yaml /tmp/meshpoint-bisect/config/

# Temporary systemd override to point the service at the worktree.
# (Or just stop the prod service and run the worktree manually under
#  the same venv. Whichever is faster.)
```

### Round 1: confirm regression exists

1. Stop production meshpoint service.
2. Run from `/tmp/meshpoint-bisect` at commit `9514daf` (stock upstream v0.7.2).
3. From MP, send 20 MT channel broadcasts (`stock test 1` through `stock test 20`), one every ~10 s.
4. Count arrivals in MPRNS's `messages` table:
   ```bash
   sudo sqlite3 /opt/meshpoint/data/concentrator.db \
     "SELECT COUNT(*) FROM messages WHERE text LIKE 'stock test%';"
   ```

**Interpretation:**
- **≥16/20 (80%+) on stock**: regression is real and in the rnsdusb commits. Proceed to Round 2.
- **<8/20 (40%) on stock**: regression is environmental (interference, antenna shift since the operator's "before modifications" data point). Stop chasing software. Look at the physical setup or RFI sources.

### Round 2: bisect

```bash
cd /tmp/meshpoint-bisect
sudo git -c safe.directory=/tmp/meshpoint-bisect bisect start
sudo git -c safe.directory=/tmp/meshpoint-bisect bisect good 9514daf
sudo git -c safe.directory=/tmp/meshpoint-bisect bisect bad rnsdusb

# For each checkout:
#   sudo systemctl restart meshpoint   (or restart the manual run)
#   wait 30 s for boot
#   send 10 MT messages from MP, count arrivals
#   if ≥8/10: git bisect good
#   else:     git bisect bad
```

Expected: 3-4 bisect steps to find the first-bad commit given ~10 HAL-touching commits between upstream and HEAD.

### Cleanup

```bash
sudo git -c safe.directory=/opt/meshpoint -C /opt/meshpoint \
  worktree remove /tmp/meshpoint-bisect
sudo systemctl restart meshpoint
```

## Once the first-bad commit is identified

- Read the diff carefully. Look specifically at the HAL boundary behavior (what packets get dropped, what registers are written, what filter masks are applied).
- Decide: revert, fix forward, or guard behind a config flag.
- Add a `tests/test_sx1302_*` test that catches the regression so it can't reappear.
- Re-soak for 1 hour to confirm.

## Related context

- LXMF latency / drops on MPRNS: separate issue, not part of this investigation. Suspects: `enable_transport = Yes / mode = full` in `~/.reticulum/config` (MPRNS is acting as a full RNS transport relay), sidecar `lxmf_inbox_dump.py` 30 s polling, CPU contention with main service.
- See CHANGELOG entry for `rnsdusb branch (May 19, 2026)` for the full feature list and other shipped fixes.
