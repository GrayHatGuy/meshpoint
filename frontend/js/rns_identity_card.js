/**
 * Radio tab - Reticulum Identity card.
 *
 * Live in Phase 2: fetches the LXMF address and display name from
 * the rnsd+lxmd stack (separate process, owned by the Pi's login
 * user, talked to via /api/reticulum/identity).
 *
 * Fields:
 *   - Display Name: lxmd config's [lxmf] display_name. Read-only here;
 *     edit ~/.lxmd/config and restart lxmd.
 *   - LXMF Address: the hash other Reticulum users send messages to.
 *     This is lxmd's delivery destination, NOT the RNS transport
 *     identity. Click to copy.
 *   - Status lamp: rnsd / lxmd liveness from systemctl.
 *
 * Falls back to a STUB state if /api/reticulum/identity returns no
 * data (rnsd/lxmd not installed, journal unreadable, etc.).
 */
class RnsIdentityCard {
    constructor(api) {
        this._api = api;
        this._root = null;
        this._refreshTimer = null;
    }

    mount(rootEl) {
        this._root = rootEl;
        rootEl.classList.add('r-card');
        rootEl.innerHTML = `
            <div class="r-card__header">
                <h3 class="r-card__title">Identity</h3>
                <span class="r-badge r-badge--mono"
                      id="rns-ident-source">--</span>
            </div>
            <div class="r-ident">
                <div class="r-ident__row">
                    <label class="r-ident__label" for="rns-display-name">Display Name</label>
                    <input class="r-input" id="rns-display-name"
                           placeholder="(not configured)" readonly />
                </div>
                <div class="r-ident__row">
                    <label class="r-ident__label" for="rns-lxmf-addr">LXMF Addr</label>
                    <input class="r-input r-input--mono" id="rns-lxmf-addr"
                           placeholder="(rnsd/lxmd not detected)"
                           readonly title="Click to copy" />
                </div>
                <div class="r-ident__hint" id="rns-ident-hint">
                    Loading...
                </div>
            </div>
        `;
        this._wire();
    }

    render(_config) {
        // Initial fetch + start periodic refresh (every 30 sec, like other cards)
        this._fetchAndRender();
        if (this._refreshTimer) clearInterval(this._refreshTimer);
        this._refreshTimer = setInterval(() => this._fetchAndRender(), 30_000);
    }

    _wire() {
        const addrInput = this._root.querySelector('#rns-lxmf-addr');
        addrInput.addEventListener('click', () => {
            if (!addrInput.value) return;
            navigator.clipboard.writeText(addrInput.value).then(
                () => this._api.toast('LXMF address copied'),
                () => {},
            );
        });
    }

    async _fetchAndRender() {
        try {
            const res = await fetch('/api/reticulum/identity');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._applyState(data);
        } catch (e) {
            this._applyStubState(e.message);
        }
    }

    _applyState(data) {
        const badge = this._root.querySelector('#rns-ident-source');
        const nameInput = this._root.querySelector('#rns-display-name');
        const addrInput = this._root.querySelector('#rns-lxmf-addr');
        const hint = this._root.querySelector('#rns-ident-hint');

        const hasAddress = !!data.address;
        const bothRunning = data.rnsd_running && data.lxmd_running;

        if (hasAddress && bothRunning) {
            badge.textContent = 'LIVE';
            badge.classList.remove('r-badge--muted');
        } else if (hasAddress) {
            badge.textContent = 'STALE';
            badge.classList.add('r-badge--muted');
        } else {
            badge.textContent = 'OFFLINE';
            badge.classList.add('r-badge--muted');
        }

        nameInput.value = data.display_name || '';
        addrInput.value = data.address ? `<${data.address}>` : '';
        hint.textContent = this._statusHint(data);
    }

    _applyStubState(errMsg) {
        const badge = this._root.querySelector('#rns-ident-source');
        const nameInput = this._root.querySelector('#rns-display-name');
        const addrInput = this._root.querySelector('#rns-lxmf-addr');
        const hint = this._root.querySelector('#rns-ident-hint');

        badge.textContent = 'STUB';
        badge.classList.add('r-badge--muted');
        nameInput.value = '';
        addrInput.value = '';
        hint.textContent = `Reticulum stack not detected. `
            + `Install via scripts/setup_rnsd.sh and reload. (${errMsg})`;
    }

    _statusHint(data) {
        const parts = [];
        parts.push(data.rnsd_running ? 'rnsd: running' : 'rnsd: stopped');
        parts.push(data.lxmd_running ? 'lxmd: running' : 'lxmd: stopped');
        if (data.address) {
            parts.push('click address to copy');
        }
        return parts.join(' · ');
    }
}

window.RnsIdentityCard = RnsIdentityCard;
