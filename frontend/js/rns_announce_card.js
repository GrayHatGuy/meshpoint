/**
 * Radio tab - Reticulum Announce Broadcast card.
 *
 * Live in Phase 1 #6:
 *   * "Send Now" button       -> POST /api/reticulum/announce
 *   * Period chip / input row -> PUT  /api/reticulum/announce
 *                                  body {period_minutes}
 *   * Countdown + last-sent   -> GET  /api/reticulum/announce
 *
 * lxmd's built-in config only supports `announce_at_start = yes`
 * (one announce on startup). Periodic re-announce is implemented by
 * the lxmf_inbox_dump.py sidecar: it polls the same announce-state
 * file we write here on a 30s cadence and shells out to
 * scripts/lxmf_announce.py when (now - last_announce_at) >= period.
 * Same script the "Send Now" button triggers via sudo on the
 * backend, so there's a single fire path.
 */
class RnsAnnounceCard {
    static PRESETS = [
        { minutes: 0,    label: 'Off' },
        { minutes: 30,   label: '30m' },
        { minutes: 60,   label: '1h' },
        { minutes: 180,  label: '3h' },
        { minutes: 360,  label: '6h' },
        { minutes: 720,  label: '12h' },
        { minutes: 1440, label: '24h' },
    ];

    constructor(api) {
        this._api = api;
        this._root = null;
        this._periodMinutes = 0;
        this._lastAnnounceAt = null;  // unix seconds or null
        this._lastAnnounceOk = null;  // bool or null
        this._tickTimer = null;
        this._refreshTimer = null;
    }

    mount(rootEl) {
        this._root = rootEl;
        rootEl.classList.add('r-card');
        rootEl.innerHTML = `
            <div class="r-card__header">
                <h3 class="r-card__title">Announce Broadcast</h3>
                <span class="status-lamp status-lamp--off" id="rns-ann-lamp">
                    <span class="status-lamp__dot"></span>
                    <span class="status-lamp__label">DISABLED</span>
                </span>
            </div>
            <div class="r-countdown">
                <div class="r-countdown__label">Next announce in</div>
                <div class="r-countdown__value" id="rns-ann-countdown">--</div>
                <div class="r-countdown__sub">
                    <span class="r-countdown__sub-item">
                        Last sent <span id="rns-ann-last-sent">--</span>
                    </span>
                    <span class="r-countdown__sub-sep">|</span>
                    <span class="r-countdown__sub-item">
                        Interval <span id="rns-ann-interval-label">--</span>
                    </span>
                </div>
            </div>
            <div class="interval-chips">
                <div class="interval-chips__row">
                    <div class="interval-chips__chips" id="rns-ann-chips"></div>
                    <div class="r-input-with-unit">
                        <input type="number" id="rns-ann-input"
                               class="r-input r-input--mono r-input--narrow"
                               min="0" max="1440" />
                        <span class="r-input-with-unit__suffix">MIN</span>
                    </div>
                </div>
                <p class="r-hint">
                    Reticulum announces broadcast this Meshpoint's LXMF
                    address so peers can route messages to us. lxmd
                    fires one at startup; this card schedules periodic
                    re-announces (peer routing caches age out otherwise).
                    Off disables the auto-cycle; "Send Now" fires one
                    regardless of the schedule.
                </p>
            </div>
            <div class="r-card__actions">
                <button class="r-btn r-btn--secondary" id="rns-ann-send-now">Send Now</button>
                <button class="r-btn r-btn--primary"   id="rns-ann-save">Save Announce</button>
            </div>
        `;
        this._renderChips();
        this._wire();
    }

    render(_config) {
        this._refreshState();
        if (this._refreshTimer) clearInterval(this._refreshTimer);
        // Backend state is small + cheap; refresh every 15s so the
        // last-sent value tracks reality when sidecar fires periodic.
        this._refreshTimer = setInterval(() => this._refreshState(), 15_000);
        if (this._tickTimer) clearInterval(this._tickTimer);
        // Countdown ticks every second locally; no API call needed.
        this._tickTimer = setInterval(() => this._renderCountdown(), 1000);
    }

    _wire() {
        this._root.querySelector('#rns-ann-send-now').addEventListener(
            'click', () => this._sendNow(),
        );
        this._root.querySelector('#rns-ann-save').addEventListener(
            'click', () => this._savePeriod(),
        );
        // Chips set the input value but don't auto-save -- explicit
        // Save click matches the existing MT NodeInfo card UX so the
        // operator doesn't fire accidental writes by browsing chips.
        this._root.querySelector('#rns-ann-chips').addEventListener('click', (ev) => {
            const btn = ev.target.closest('.r-chip');
            if (!btn) return;
            const min = parseInt(btn.dataset.min, 10);
            const input = this._root.querySelector('#rns-ann-input');
            input.value = String(min);
            this._reflectChipSelection(min);
        });
    }

    _renderChips() {
        const host = this._root.querySelector('#rns-ann-chips');
        host.innerHTML = RnsAnnounceCard.PRESETS.map((p) => {
            const cls = p.minutes === 0 ? 'r-chip r-chip--off' : 'r-chip';
            return `<button class="${cls}" data-min="${p.minutes}">${p.label}</button>`;
        }).join('');
    }

    _reflectChipSelection(min) {
        this._root.querySelectorAll('#rns-ann-chips .r-chip').forEach((btn) => {
            btn.classList.toggle(
                'r-chip--active',
                parseInt(btn.dataset.min, 10) === min,
            );
        });
    }

    async _refreshState() {
        try {
            const res = await fetch('/api/reticulum/announce');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._periodMinutes  = Number(data.period_minutes || 0);
            this._lastAnnounceAt = data.last_announce_at;   // unix sec or null
            this._lastAnnounceOk = data.last_announce_ok;
            this._applyToUI();
        } catch (e) {
            // Soft-fail: leave whatever's on screen.
        }
    }

    _applyToUI() {
        const input = this._root.querySelector('#rns-ann-input');
        input.value = String(this._periodMinutes);
        this._reflectChipSelection(this._periodMinutes);

        const lamp = this._root.querySelector('#rns-ann-lamp');
        const lampLabel = lamp.querySelector('.status-lamp__label');
        if (this._periodMinutes > 0) {
            lamp.classList.remove('status-lamp--off');
            lamp.classList.add('status-lamp--on');
            lampLabel.textContent = 'AUTO';
        } else {
            lamp.classList.remove('status-lamp--on');
            lamp.classList.add('status-lamp--off');
            lampLabel.textContent = 'DISABLED';
        }

        const intervalLabel = this._root.querySelector('#rns-ann-interval-label');
        intervalLabel.textContent = this._periodMinutes > 0
            ? this._fmtMinutes(this._periodMinutes)
            : 'off';

        const lastSent = this._root.querySelector('#rns-ann-last-sent');
        if (this._lastAnnounceAt) {
            lastSent.textContent = this._fmtRelative(this._lastAnnounceAt * 1000);
            if (this._lastAnnounceOk === false) {
                lastSent.textContent += ' (failed)';
            }
        } else {
            lastSent.textContent = 'never';
        }

        this._renderCountdown();
    }

    _renderCountdown() {
        const out = this._root.querySelector('#rns-ann-countdown');
        if (!out) return;
        if (!this._periodMinutes) { out.textContent = '--'; return; }
        if (!this._lastAnnounceAt) { out.textContent = 'pending'; return; }
        const dueAt = (this._lastAnnounceAt + this._periodMinutes * 60) * 1000;
        const remainingMs = dueAt - Date.now();
        if (remainingMs <= 0) {
            out.textContent = 'due now';
            return;
        }
        const secs = Math.floor(remainingMs / 1000);
        if (secs < 60) { out.textContent = `${secs}s`; return; }
        const m = Math.floor(secs / 60);
        if (m < 60) { out.textContent = `${m}m ${secs % 60}s`; return; }
        const h = Math.floor(m / 60);
        out.textContent = `${h}h ${m % 60}m`;
    }

    async _sendNow() {
        const btn = this._root.querySelector('#rns-ann-send-now');
        btn.disabled = true;
        const orig = btn.textContent;
        btn.textContent = 'Sending…';
        try {
            const res = await fetch('/api/reticulum/announce', { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(data.detail || `HTTP ${res.status}`);
            }
            this._api.toast('Announce sent');
        } catch (e) {
            this._api.toast(`Announce failed: ${e.message}`);
        } finally {
            btn.disabled = false;
            btn.textContent = orig;
            // Re-fetch so the countdown + last-sent reflect the fire.
            this._refreshState();
        }
    }

    async _savePeriod() {
        const input = this._root.querySelector('#rns-ann-input');
        const val = Number(input.value);
        if (!Number.isFinite(val) || val < 0 || val > 1440) {
            this._api.toast('Period must be 0..1440 minutes');
            return;
        }
        const btn = this._root.querySelector('#rns-ann-save');
        btn.disabled = true;
        try {
            const res = await fetch('/api/reticulum/announce', {
                method:  'PUT',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({period_minutes: Math.round(val)}),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
            this._api.toast(
                val === 0 ? 'Auto-announce disabled'
                          : `Auto-announce every ${this._fmtMinutes(val)}`,
            );
            this._refreshState();
        } catch (e) {
            this._api.toast(`Save failed: ${e.message}`);
        } finally {
            btn.disabled = false;
        }
    }

    _fmtMinutes(m) {
        if (m < 60) return `${m}m`;
        if (m % 60 === 0) return `${m / 60}h`;
        return `${Math.floor(m / 60)}h ${m % 60}m`;
    }

    _fmtRelative(ms) {
        const ago = Math.max(0, Math.floor((Date.now() - ms) / 1000));
        if (ago < 60) return `${ago}s ago`;
        if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
        if (ago < 86400) return `${Math.floor(ago / 3600)}h ago`;
        return `${Math.floor(ago / 86400)}d ago`;
    }
}

window.RnsAnnounceCard = RnsAnnounceCard;
