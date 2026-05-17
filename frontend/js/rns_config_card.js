/**
 * Radio tab - Reticulum Radio Configuration card.
 *
 * Mirrors the Meshtastic radio config layout but reads from
 * config.capture.rnode_usb (which holds the parameters Meshpoint uses
 * to monitor Reticulum traffic, both via the USB RNode source and via
 * the SX1302 multi-SF demod when concentrator_multi_protocol is on).
 *
 * Until /api/config exposes a writable endpoint for these fields the
 * card is read-only and shows a "Edit local.yaml then restart" hint.
 * The values still update live whenever the underlying config payload
 * changes, so the card stays accurate after a service restart.
 */
class RnsConfigCard {
    constructor(api) {
        this._api = api;
        this._root = null;
    }

    mount(rootEl) {
        this._root = rootEl;
        rootEl.classList.add('r-card');
        rootEl.innerHTML = `
            <div class="r-card__header">
                <h3 class="r-card__title">Radio Configuration</h3>
                <span class="r-card__subtitle" id="rns-config-subtitle">--</span>
            </div>
            <div class="config-stack">
                <div class="config-pane">
                    <div class="config-pane__label">Network</div>
                    <div class="config-pane__inputs">
                        <div class="r-field">
                            <label class="r-field__label" for="rns-network">Network</label>
                            <input type="text" class="r-input" id="rns-network"
                                   value="Reticulum" disabled />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="rns-source">Capture source</label>
                            <input type="text" class="r-input" id="rns-source"
                                   placeholder="--" disabled />
                        </div>
                    </div>
                </div>
                <div class="config-pane">
                    <div class="config-pane__label">Tuning</div>
                    <div class="config-pane__inputs">
                        <div class="r-field">
                            <label class="r-field__label" for="rns-freq">Frequency (MHz)</label>
                            <input type="text" class="r-input r-input--mono r-input--narrow"
                                   id="rns-freq" disabled />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="rns-tx-power">TX power (dBm)</label>
                            <input type="text" class="r-input r-input--mono r-input--narrow"
                                   id="rns-tx-power" disabled />
                        </div>
                    </div>
                </div>
            </div>
            <div class="readout-strip">
                <div class="readout-strip__label">Computed</div>
                <div class="r-readout">
                    <span class="r-readout__label">SF</span>
                    <span class="r-readout__value" id="rns-sf">--</span>
                </div>
                <div class="r-readout">
                    <span class="r-readout__label">BW</span>
                    <span class="r-readout__value" id="rns-bw">--</span>
                </div>
                <div class="r-readout">
                    <span class="r-readout__label">CR</span>
                    <span class="r-readout__value" id="rns-cr">--</span>
                </div>
                <div class="r-readout">
                    <span class="r-readout__label">Sync</span>
                    <span class="r-readout__value" id="rns-sync">--</span>
                </div>
                <div class="r-readout">
                    <span class="r-readout__label">Source</span>
                    <span class="r-readout__value" id="rns-source-tag">--</span>
                </div>
            </div>
            <p class="r-hint">
                Reticulum radio parameters are sourced from
                <code>capture.rnode_usb</code> in <code>local.yaml</code>.
                Edit there and restart to change. A future revision will
                make this panel writable.
            </p>
            <p class="r-hint" id="rns-config-stack-status">
                Messaging stack: <span id="rns-stack-state">unknown</span>
            </p>
        `;
    }

    render(config) {
        // Pull rnode_usb radio params; fall back gracefully if the key
        // isn't present (older config payloads predate Tier 1).
        const cap = config.capture || {};
        const rnode = cap.rnode_usb || {};
        const sources = cap.sources || [];
        const multiProto = cap.concentrator_multi_protocol === true;

        const freqMhz = rnode.frequency_hz
            ? (rnode.frequency_hz / 1_000_000).toFixed(3)
            : '--';
        const bwKhz = rnode.bandwidth_hz
            ? Math.round(rnode.bandwidth_hz / 1_000)
            : '--';
        const sf = rnode.spreading_factor ?? '--';
        const cr = rnode.coding_rate ? `4/${rnode.coding_rate}` : '--';
        const txp = rnode.tx_power ?? '--';
        const sync = rnode.sync_word !== undefined
            ? `0x${rnode.sync_word.toString(16).toUpperCase().padStart(2, '0')}`
            : '0x42';

        // Decide which physical source is currently capturing RNS.
        // USB is active either when explicitly listed in sources OR when
        // auto_detect is on (the server.py auto-detect fallback adds it
        // even if the user comments rnode_usb out of `sources`).
        const usbActive = sources.includes('rnode_usb')
            || rnode.auto_detect === true;
        const concActive = multiProto;
        let sourceLabel = 'none';
        if (usbActive && concActive) sourceLabel = 'USB RNode + SX1302';
        else if (usbActive) sourceLabel = 'USB RNode';
        else if (concActive) sourceLabel = 'SX1302 (multi-SF)';

        this._root.querySelector('#rns-source').value = sourceLabel;
        this._root.querySelector('#rns-freq').value = freqMhz;
        this._root.querySelector('#rns-tx-power').value = txp;
        this._root.querySelector('#rns-sf').textContent = `SF${sf}`;
        this._root.querySelector('#rns-bw').textContent = `${bwKhz} kHz`;
        this._root.querySelector('#rns-cr').textContent = cr;
        this._root.querySelector('#rns-sync').textContent = sync;
        this._root.querySelector('#rns-source-tag').textContent = sourceLabel;

        const subtitle = this._root.querySelector('#rns-config-subtitle');
        subtitle.textContent = `${freqMhz} MHz / SF${sf} / BW${bwKhz}`;

        // Phase 2 addition: also reflect rnsd/lxmd liveness so the
        // operator can see at a glance whether messaging is alive.
        this._refreshStackState();
    }

    async _refreshStackState() {
        const el = this._root.querySelector('#rns-stack-state');
        if (!el) return;
        try {
            const res = await fetch('/api/reticulum/status');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const rnsd = data.rnsd_running ? 'rnsd up' : 'rnsd down';
            const lxmd = data.lxmd_running ? 'lxmd up' : 'lxmd down';
            const ifaces = (data.interfaces || []).length;
            el.textContent = `${rnsd} · ${lxmd} · ${ifaces} interface(s)`;
        } catch (e) {
            el.textContent = 'not detected (rnsd/lxmd not installed)';
        }
    }
}

window.RnsConfigCard = RnsConfigCard;
