/**
 * Radio tab - Reticulum Channels card.
 *
 * Titled "Channels" to mirror the Meshtastic Channels card. In
 * Reticulum terminology these rows are *destinations* (identity
 * hashes), but presenting them under the "Channels" label keeps the
 * UI parallel to the MT side and matches operator expectations.
 *
 * Until Tier 2 lands the local destination row is a stub. The peer
 * rows below it are populated from the existing nodes table filtered
 * to protocol = "reticulum", so they reflect what we have actually
 * heard on the air. This gives the operator something useful to look
 * at today while still mirroring the Channels card's visual layout.
 */
class RnsDestinationsCard {
    constructor(api) {
        this._api = api;
        this._root = null;
    }

    mount(rootEl) {
        this._root = rootEl;
        rootEl.classList.add('r-card');
        rootEl.innerHTML = `
            <div class="r-card__header">
                <h3 class="r-card__title">Channels</h3>
                <span class="r-card__subtitle" id="rns-dest-subtitle">
                    -- discovered
                </span>
            </div>
            <table class="ch-table">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Name / Class</th>
                        <th>Destination Hash</th>
                        <th>Route</th>
                        <th>Last Heard</th>
                        <th>RSSI</th>
                    </tr>
                </thead>
                <tbody id="rns-dest-body"></tbody>
            </table>
            <div class="r-card__actions">
                <button class="r-btn r-btn--secondary"
                        id="rns-dest-refresh">Refresh</button>
                <button class="r-btn r-btn--primary"
                        id="rns-dest-add" disabled
                        title="Available in Tier 2">+ Add Destination</button>
            </div>
            <p class="r-hint">
                Reticulum's "channels" are destinations: each row is an
                identity hash heard on the air. Row 0 is this Meshpoint's
                own LXMF address; subsequent rows are peers that have
                broadcast an announce within the last 24 hours.
            </p>
        `;
        this._wire();
    }

    render(_config) {
        // Initial render - kick off a peers fetch.
        this._loadPeers();
    }

    _wire() {
        this._root.querySelector('#rns-dest-refresh').addEventListener(
            'click', () => this._loadPeers(),
        );
        // Phase 1 #2b: click any peer NAME cell to open inline edit.
        // Phase 1 #5: click anywhere ELSE on a peer row to jump to
        //             that peer's conversation on the Messages tab.
        // Local row (data-local="1") is excluded from both -- it
        // represents ourselves (renaming self is via ~/.lxmd/config
        // display_name; messaging yourself isn't useful).
        this._root.querySelector('#rns-dest-body').addEventListener(
            'click', (ev) => {
                const row = ev.target.closest('.rns-edit-row');
                if (!row || row.dataset.local === '1') return;

                // Name cell -> edit name (preserves Phase 1 #2b UX).
                if (ev.target.closest('.rns-name-cell')) {
                    this._openInlineEdit(row);
                    return;
                }

                // Anywhere else on the row -> open conversation. We
                // only do this for LXMF-class peers; clicking a relay
                // or transport node would be misleading (you can't
                // message them, they don't run an LXMF endpoint).
                const cls = row.dataset.class || 'unknown';
                if (cls !== 'lxmf') return;
                const hash = row.dataset.hash || '';
                const name = row.dataset.currentName || '';
                if (window.messagingPanel
                        && typeof window.messagingPanel.openRnsConversation === 'function') {
                    window.messagingPanel.openRnsConversation(hash, name);
                }
            },
        );
    }

    _openInlineEdit(row) {
        const cell = row.querySelector('.rns-name-cell');
        if (!cell || cell.querySelector('.rns-edit-input')) return; // already open
        const hash    = row.dataset.hash || '';
        const current = row.dataset.currentName || '';
        const source  = row.dataset.source || 'none';
        const isOperatorSet = (source === 'operator');

        // Replace the cell content with an inline edit form. Single
        // text input + Save / Cancel / (if operator-set) Revert. Enter
        // saves, Escape cancels. Keeps the cell width stable so the
        // table doesn't jump when the user opens it.
        cell.innerHTML = `
            <span class="rns-edit-form">
                <input type="text" class="r-input rns-edit-input"
                       value="${this._esc(current)}"
                       placeholder="Nickname"
                       maxlength="64" />
                <button class="r-btn r-btn--primary rns-edit-save">Save</button>
                <button class="r-btn r-btn--secondary rns-edit-cancel">Cancel</button>
                ${isOperatorSet
                    ? '<button class="r-btn r-btn--secondary rns-edit-revert" title="Remove operator override; revert to announce name">Revert</button>'
                    : ''}
            </span>
        `;
        const input = cell.querySelector('.rns-edit-input');
        input.focus();
        input.select();

        const close = () => this._loadPeers();   // re-render fresh

        cell.querySelector('.rns-edit-cancel').addEventListener(
            'click', close,
        );
        cell.querySelector('.rns-edit-save').addEventListener(
            'click', () => this._saveContact(hash, input.value, close),
        );
        const revertBtn = cell.querySelector('.rns-edit-revert');
        if (revertBtn) {
            revertBtn.addEventListener(
                'click', () => this._deleteContact(hash, close),
            );
        }
        input.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter')  this._saveContact(hash, input.value, close);
            if (ev.key === 'Escape') close();
        });
    }

    async _saveContact(hash, nickname, done) {
        const nick = (nickname || '').trim();
        if (!nick) {
            this._api.toast('Nickname cannot be blank');
            return;
        }
        try {
            const res = await fetch(`/api/reticulum/contacts/${hash}`, {
                method:  'PUT',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({nickname: nick}),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            this._api.toast(`Saved "${nick}"`);
        } catch (e) {
            this._api.toast(`Save failed: ${e.message}`);
        }
        done();
    }

    async _deleteContact(hash, done) {
        try {
            const res = await fetch(`/api/reticulum/contacts/${hash}`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            this._api.toast('Reverted to announce name');
        } catch (e) {
            this._api.toast(`Revert failed: ${e.message}`);
        }
        done();
    }

    async _loadPeers() {
        const body = this._root.querySelector('#rns-dest-body');
        const subtitle = this._root.querySelector('#rns-dest-subtitle');
        await this._refreshLocalRow();
        const localRow = this._renderLocalRow();

        try {
            // /api/reticulum/peers returns LXMF-aware peers parsed from
            // rnsd's announce log. Each entry has hash, hops, RSSI, SNR,
            // last_heard, and via-relay info -- much richer than the
            // generic /api/nodes view we used in the stub version.
            const res = await fetch('/api/reticulum/peers?limit=50');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const peers = data.peers || [];

            const peerRows = peers.map((p, i) => this._peerRow(p, i + 1)).join('');
            body.innerHTML = localRow + peerRows;
            subtitle.textContent = `${peers.length} peer(s) heard`;
        } catch (e) {
            body.innerHTML = localRow + `
                <tr><td colspan="6" class="ch-table__hash">
                    rnsd peer list unavailable (${this._esc(e.message)}).
                    Install via scripts/setup_rnsd.sh to activate.
                </td></tr>
            `;
            subtitle.textContent = 'rnsd offline';
        }
    }

    _renderLocalRow() {
        // Local-identity row. Hash populated asynchronously from
        // /api/reticulum/identity (see _refreshLocalRow). If the identity
        // hasn't been fetched yet, show a placeholder; subsequent
        // refresh ticks will replace it.
        const addr = this._localAddress
            ? this._esc(this._localAddress.slice(0, 16) + '…')
            : '(loading...)';
        const title = this._localAddress ? this._esc(this._localAddress) : '';
        // Local row gets a fixed (self) name + lxmf class -- always.
        return `
            <tr class="ch-table__row" data-local="1">
                <td class="ch-table__idx">0</td>
                <td>
                    <em>(this Meshpoint)</em>
                    <span class="rns-class rns-class--lxmf">lxmf</span>
                </td>
                <td class="ch-table__hash" title="${title}">${addr}</td>
                <td>--</td>
                <td>--</td>
                <td>--</td>
            </tr>
        `;
    }

    async _refreshLocalRow() {
        try {
            const res = await fetch('/api/reticulum/identity');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._localAddress = data.address || null;
        } catch (e) {
            this._localAddress = null;
        }
    }

    _peerRow(peer, idx) {
        // Peer shape from /api/reticulum/peers (Phase 1 #2 + #2b):
        //   { hash, hops, via, interface, last_heard, rssi, snr,
        //     display_name, display_name_source, class, is_lxmf }
        //
        // display_name_source ("operator"|"classifier"|"none") tells
        // us whether the operator has overridden the auto-discovered
        // name -- shown as a small pencil icon.
        const hash = peer.hash || '--';
        const cls = peer.class || 'unknown';
        const name = peer.display_name;
        const source = peer.display_name_source || 'none';

        // Operator-set: show a pencil to indicate "you edited this".
        // Click anywhere on the name cell (including the name itself,
        // or the pencil/edit affordance below) opens the inline edit
        // input. We tag the cell with data attrs so the click handler
        // wired in _wire() can find the hash and current name.
        const pencil = (source === 'operator')
            ? ' <span class="rns-edit-mark" title="Operator-set name">✎</span>'
            : ' <span class="rns-edit-mark rns-edit-mark--hint" title="Click to set a name">✎</span>';

        const nameCell = name
            ? `<span class="rns-name-text">${this._esc(name)}</span>${pencil} <span class="rns-class rns-class--${cls}">${cls}</span>`
            : `<em class="rns-class__empty">(no name)</em>${pencil} <span class="rns-class rns-class--${cls}">${cls}</span>`;

        // Route: hops + via-relay summary. Previously the "Name" column,
        // moved here so the new column ordering reads name -> hash -> route.
        const hops = peer.hops != null
            ? `${peer.hops} hop${peer.hops === 1 ? '' : 's'}` : '';
        const viaTag = peer.via ? ` via ${peer.via.slice(0, 8)}…` : '';
        const routeText = (hops + viaTag).trim() || 'unknown';

        const lastHeard = this._fmtTime(peer.last_heard);
        const rssi = (peer.rssi != null)
            ? `${peer.rssi.toFixed(1)} dBm` : '--';

        return `
            <tr class="ch-table__row rns-edit-row" data-class="${cls}"
                data-hash="${this._esc(hash)}"
                data-current-name="${this._esc(name || '')}"
                data-source="${source}">
                <td class="ch-table__idx">${idx}</td>
                <td class="rns-name-cell">${nameCell}</td>
                <td class="ch-table__hash" title="${this._esc(hash)}">
                    ${this._esc(hash.length > 16 ? hash.slice(0, 16) + '…' : hash)}
                </td>
                <td>${this._esc(routeText)}</td>
                <td>${lastHeard}</td>
                <td>${rssi}</td>
            </tr>
        `;
    }

    _fmtTime(iso) {
        if (!iso) return '--';
        try {
            const t = new Date(iso);
            const ago = Math.floor((Date.now() - t.getTime()) / 1000);
            if (ago < 60) return `${ago}s ago`;
            if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
            if (ago < 86400) return `${Math.floor(ago / 3600)}h ago`;
            return `${Math.floor(ago / 86400)}d ago`;
        } catch (e) {
            return '--';
        }
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str || '';
        return el.innerHTML;
    }
}

window.RnsDestinationsCard = RnsDestinationsCard;
