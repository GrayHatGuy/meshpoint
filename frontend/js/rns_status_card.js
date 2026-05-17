/**
 * Radio tab - Reticulum Stack Status card (Phase 1 #6b).
 *
 * Live diagnostic panel for the Reticulum stack: rnsd + lxmd
 * service state, per-interface details from rnstatus, and a
 * Restart Stack button that bounces rnsd (lxmd auto-cascades via
 * systemd PartOf=rnsd.service).
 *
 * Sources data from the existing GET /api/reticulum/status
 * endpoint -- no new backend needed for read. The restart button
 * hits a new POST /api/reticulum/restart that shells out to
 * `sudo systemctl restart rnsd.service` via a narrow sudoers
 * grant (see scripts/templates/meshpoint-lxmf.sudoers).
 *
 * Auto-refresh cadence: 15s on tab open, plus a manual Refresh
 * button. The card stays cheap to render so polling doesn't
 * waste CPU even when the operator leaves the Radio tab open.
 */
class RnsStatusCard {
    constructor(api) {
        this._api = api;
        this._root = null;
        this._timer = null;
    }

    mount(rootEl) {
        this._root = rootEl;
        rootEl.classList.add('r-card');
        rootEl.innerHTML = `
            <div class="r-card__header">
                <h3 class="r-card__title">Stack Status</h3>
                <span class="r-badge r-badge--mono" id="rns-stat-badge">--</span>
            </div>
            <div class="rns-stat" id="rns-stat-body">
                <div class="rns-stat__loading">Loading rnstatus...</div>
            </div>
            <div class="r-card__actions">
                <button class="r-btn r-btn--secondary" id="rns-stat-refresh">Refresh</button>
                <button class="r-btn r-btn--warn"      id="rns-stat-restart"
                        title="Restart rnsd (lxmd cascades)">Restart Stack</button>
            </div>
            <p class="r-hint">
                Config files for manual edits:<br>
                <code>~/.reticulum/config</code> (rnsd) ·
                <code>~/.lxmd/config</code> (lxmd).<br>
                Restart Stack briefly disconnects all RNS clients
                (MeshChat, sidecar, in-flight LXMF sends). Service
                comes back online in ~5 seconds.
            </p>
        `;
        this._wire();
    }

    render(_config) {
        this._fetchAndRender();
        if (this._timer) clearInterval(this._timer);
        this._timer = setInterval(() => this._fetchAndRender(), 15_000);
    }

    _wire() {
        this._root.querySelector('#rns-stat-refresh').addEventListener(
            'click', () => this._fetchAndRender(),
        );
        this._root.querySelector('#rns-stat-restart').addEventListener(
            'click', () => this._restart(),
        );
    }

    async _fetchAndRender() {
        try {
            const res = await fetch('/api/reticulum/status');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._applyState(data);
        } catch (e) {
            this._applyState(null, e.message);
        }
    }

    _applyState(data, errMsg) {
        const badge = this._root.querySelector('#rns-stat-badge');
        const body  = this._root.querySelector('#rns-stat-body');
        if (!data) {
            badge.textContent = 'OFFLINE';
            badge.classList.add('r-badge--muted');
            body.innerHTML =
                `<div class="rns-stat__loading">Status unavailable (${this._esc(errMsg || '')})</div>`;
            return;
        }
        const both = data.rnsd_running && data.lxmd_running;
        if (both) {
            badge.textContent = 'UP';
            badge.classList.remove('r-badge--muted');
        } else if (data.rnsd_running || data.lxmd_running) {
            badge.textContent = 'PARTIAL';
            badge.classList.add('r-badge--muted');
        } else {
            badge.textContent = 'DOWN';
            badge.classList.add('r-badge--muted');
        }

        const ifaces = Array.isArray(data.interfaces) ? data.interfaces : [];

        body.innerHTML = `
            <div class="rns-stat__svcrow">
                <span class="rns-stat__svc">
                    <span class="rns-stat__lamp ${data.rnsd_running ? 'rns-stat__lamp--on' : 'rns-stat__lamp--off'}"></span>
                    rnsd  <em>${data.rnsd_running ? 'active' : 'stopped'}</em>
                </span>
                <span class="rns-stat__svc">
                    <span class="rns-stat__lamp ${data.lxmd_running ? 'rns-stat__lamp--on' : 'rns-stat__lamp--off'}"></span>
                    lxmd  <em>${data.lxmd_running ? 'active' : 'stopped'}</em>
                </span>
            </div>
            ${ifaces.length === 0
                ? '<div class="rns-stat__loading">No interfaces reported by rnstatus.</div>'
                : `<div class="rns-stat__ifaces">${ifaces.map((i) => this._ifaceBlock(i)).join('')}</div>`
            }
        `;
    }

    _ifaceBlock(iface) {
        // iface = { interface: "<header line>", fields: {key: value, ...} }
        const fields = iface.fields || {};
        const rows = Object.entries(fields).map(
            ([k, v]) => `
                <div class="rns-stat__field">
                    <span class="rns-stat__field-key">${this._esc(k)}</span>
                    <span class="rns-stat__field-val">${this._esc(v)}</span>
                </div>
            `
        ).join('');
        return `
            <div class="rns-stat__iface">
                <div class="rns-stat__iface-head">${this._esc(iface.interface || '')}</div>
                ${rows}
            </div>
        `;
    }

    async _restart() {
        if (!confirm(
            'Restart rnsd? lxmd will cascade.\n\n'
            + 'Briefly disconnects every RNS client on this Pi '
            + '(MeshChat, sidecar, in-flight LXMF sends).\n\n'
            + 'Comes back online in ~5 seconds.'
        )) return;

        const btn = this._root.querySelector('#rns-stat-restart');
        btn.disabled = true;
        const orig = btn.textContent;
        btn.textContent = 'Restarting...';

        try {
            const res = await fetch('/api/reticulum/restart', { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(data.detail || `HTTP ${res.status}`);
            }
            this._api.toast('rnsd restarted');
            // Give services a moment to settle, then re-poll status.
            setTimeout(() => this._fetchAndRender(), 4000);
        } catch (e) {
            this._api.toast(`Restart failed: ${e.message}`);
        } finally {
            btn.disabled = false;
            btn.textContent = orig;
        }
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str == null ? '' : String(str);
        return el.innerHTML;
    }
}

window.RnsStatusCard = RnsStatusCard;
