/**
 * Rich node card list for the dashboard side panel.
 * Renders Meshtastic-app-style cards with avatar, signal,
 * telemetry chips, role, hardware, and online status.
 */
class NodeCards {
    constructor(containerId, onCardClick) {
        this._container = document.getElementById(containerId);
        this._onCardClick = onCardClick;
        this._nodes = [];
        this._searchQuery = '';
        this._sortBy = 'last_heard';

        // Phase 1 #3: protocol filter. 'all' shows everything;
        // 'mt'/'mc'/'reticulum' filter to one stack. Stored in
        // localStorage so the operator's last choice survives a
        // page refresh.
        this._protoFilter = localStorage.getItem('nc-proto-filter') || 'all';

        const searchEl = document.getElementById('node-search');
        if (searchEl) {
            searchEl.addEventListener('input', (e) => {
                this._searchQuery = e.target.value.toLowerCase();
                this._render();
            });
        }
        this._mountFilterChips();
    }

    _mountFilterChips() {
        // Inject the filter chip row above the container body. Done
        // here in JS (not the static index.html) so the chips travel
        // with whichever container this card is bound to -- keeps
        // index.html stable.
        if (!this._container) return;
        const chipRow = document.createElement('div');
        chipRow.className = 'nc-filter';
        chipRow.innerHTML = `
            <button class="nc-filter__chip" data-pf="all">All</button>
            <button class="nc-filter__chip" data-pf="meshtastic">MT</button>
            <button class="nc-filter__chip" data-pf="meshcore">MC</button>
            <button class="nc-filter__chip" data-pf="reticulum">RNS</button>
        `;
        this._container.parentNode.insertBefore(chipRow, this._container);
        chipRow.querySelectorAll('.nc-filter__chip').forEach((btn) => {
            btn.addEventListener('click', () => {
                this._protoFilter = btn.dataset.pf;
                localStorage.setItem('nc-proto-filter', this._protoFilter);
                this._render();
            });
        });
        this._chipRow = chipRow;
        this._reflectActiveChip();
    }

    _reflectActiveChip() {
        if (!this._chipRow) return;
        this._chipRow.querySelectorAll('.nc-filter__chip').forEach((b) => {
            b.classList.toggle(
                'nc-filter__chip--active',
                b.dataset.pf === this._protoFilter,
            );
        });
    }

    loadNodes(nodes) {
        this._nodes = nodes;
        this._render();
    }

    updateFromPacket(packet) {
        if (!packet.source_id) return;
        const idx = this._nodes.findIndex(n => n.node_id === packet.source_id);
        if (idx >= 0) {
            const n = this._nodes[idx];
            n.last_heard = new Date().toISOString();
            if (packet.rssi != null) n.latest_rssi = packet.rssi;
            if (packet.snr != null) n.latest_snr = packet.snr;
            if (packet.decoded_payload?.long_name) {
                n.long_name = packet.decoded_payload.long_name;
            }
            this._nodes.splice(idx, 1);
            this._nodes.unshift(n);
        } else {
            this._nodes.unshift({
                node_id: packet.source_id,
                long_name: packet.decoded_payload?.long_name || null,
                short_name: packet.decoded_payload?.short_name || null,
                protocol: packet.protocol || 'meshtastic',
                last_heard: new Date().toISOString(),
                latest_rssi: packet.rssi,
                latest_snr: packet.snr,
            });
        }
        this._render();
    }

    _render() {
        let filtered = this._nodes;
        // Phase 1 #3: protocol filter applied before search so the
        // count in the empty-state message reflects the active stack.
        if (this._protoFilter && this._protoFilter !== 'all') {
            filtered = filtered.filter(n =>
                (n.protocol || '').toLowerCase() === this._protoFilter
            );
        }
        if (this._searchQuery) {
            filtered = filtered.filter(n => {
                const name = (n.long_name || n.short_name || n.display_name || '').toLowerCase();
                const id = (n.node_id || '').toLowerCase();
                return name.includes(this._searchQuery) || id.includes(this._searchQuery);
            });
        }
        this._reflectActiveChip();

        if (filtered.length === 0) {
            this._container.innerHTML =
                '<div class="nc-empty">No nodes found</div>';
            return;
        }

        this._container.innerHTML = filtered.map(n => this._buildCard(n)).join('');

        this._container.querySelectorAll('.nc-card').forEach(el => {
            el.addEventListener('click', () => {
                const nodeId = el.dataset.nodeId;
                const node = this._nodes.find(n => n.node_id === nodeId);
                if (node && this._onCardClick) this._onCardClick(node);
            });
        });
    }

    _buildCard(n) {
        const name = this._esc(n.display_name || n.long_name || n.short_name || n.node_id || '--');
        const shortLabel = this._esc(n.short_name || (n.node_id || '').slice(-4)).toUpperCase();
        const avatarColor = this._hashColor(n.node_id || '');
        const proto = n.protocol || 'meshtastic';
        // Phase 1 #1b: dedicated RNS badge so Reticulum nodes don't
        // masquerade as Meshtastic. peer_class (lxmf / relay /
        // propagation / transport / rns_service) comes from the
        // enrichment join in /api/nodes; we suffix it to RNS so
        // operators can spot relays/propagation nodes among LXMF
        // correspondents at a glance.
        let protoBadge;
        if (proto === 'meshcore') {
            protoBadge = 'MC';
        } else if (proto === 'reticulum') {
            const cls = n.peer_class || 'unknown';
            protoBadge = (cls === 'unknown' || cls === 'lxmf')
                ? 'RNS' : `RNS·${cls}`;
        } else {
            protoBadge = 'MT';
        }
        const online = this._isOnline(n.last_heard);
        const onlineDot = online
            ? '<span class="nc-online nc-online--on" title="Online"></span>'
            : '<span class="nc-online nc-online--off" title="Offline"></span>';

        const signal = this._buildSignal(n);
        const telemetry = this._buildTelemetry(n);
        const meta = this._buildMeta(n);

        return `<div class="nc-card" data-node-id="${this._esc(n.node_id)}">
            <div class="nc-card__top">
                <div class="nc-avatar" style="background:${avatarColor}">${shortLabel}</div>
                <div class="nc-card__identity">
                    <div class="nc-card__name">${onlineDot} ${name}</div>
                    <div class="nc-card__heard">${this._timeAgo(n.last_heard)}</div>
                </div>
                <span class="nc-proto nc-proto--${proto}">${protoBadge}</span>
            </div>
            ${signal}
            ${telemetry}
            ${meta}
        </div>`;
    }

    _buildSignal(n) {
        const parts = [];
        const rssi = n.latest_rssi ?? n.rssi;
        const snr = n.latest_snr ?? n.snr;

        if (rssi != null) {
            const q = this._signalQuality(rssi);
            parts.push(`<span class="nc-chip nc-chip--signal nc-chip--${q.cls}">
                ${this._signalBars(rssi)} ${rssi.toFixed(0)} dBm</span>`);
            if (snr != null) {
                parts.push(`<span class="nc-chip">SNR ${snr.toFixed(1)} dB</span>`);
            }
            parts.push(`<span class="nc-chip nc-chip--quality nc-chip--${q.cls}">${q.label}</span>`);
        }

        if (n.latest_hops != null && n.latest_hops > 0) {
            parts.push(`<span class="nc-chip">${n.latest_hops} hop${n.latest_hops > 1 ? 's' : ''}</span>`);
        }

        // Phase 1 #3: Reticulum announce route info. `hops` here is
        // populated by the backend from the rnsd journal (NOT the
        // per-packet hop_limit), so it reflects how many transport
        // relays the latest announce traversed. via= shows the first
        // upstream relay -- useful when you have multiple gateways.
        if ((n.protocol || '').toLowerCase() === 'reticulum' && n.hops != null) {
            const hopLabel = n.hops === 0
                ? 'direct'
                : `${n.hops} hop${n.hops === 1 ? '' : 's'}`;
            const viaSuffix = n.via ? ` via ${n.via.slice(0, 8)}…` : '';
            parts.push(`<span class="nc-chip nc-chip--rns">${hopLabel}${viaSuffix}</span>`);
        }

        return parts.length
            ? `<div class="nc-card__row">${parts.join('')}</div>`
            : '';
    }

    _buildTelemetry(n) {
        const parts = [];
        const voltage = n.latest_voltage;
        const battery = n.latest_battery;
        const temp = n.latest_temperature;
        const humidity = n.latest_humidity;
        const chUtil = n.latest_channel_util;
        const airUtil = n.latest_air_util;
        const alt = n.altitude;

        if (voltage != null) {
            parts.push(`<span class="nc-chip nc-chip--telem">&#9889; ${voltage.toFixed(2)}V</span>`);
        }
        if (battery != null && battery > 0) {
            parts.push(`<span class="nc-chip nc-chip--telem">${this._batteryIcon(battery)} ${battery}%</span>`);
        }
        if (alt != null) {
            parts.push(`<span class="nc-chip nc-chip--telem">&#9650; ${Math.round(alt)} ft</span>`);
        }
        if (temp != null) {
            parts.push(`<span class="nc-chip nc-chip--telem">&#127777; ${temp.toFixed(1)}&deg;F</span>`);
        }
        if (humidity != null) {
            parts.push(`<span class="nc-chip nc-chip--telem">&#128167; ${humidity.toFixed(0)}%</span>`);
        }
        if (chUtil != null) {
            parts.push(`<span class="nc-chip nc-chip--telem">ChUtil ${chUtil.toFixed(1)}%</span>`);
        }
        if (airUtil != null) {
            parts.push(`<span class="nc-chip nc-chip--telem">AirUtil ${airUtil.toFixed(1)}%</span>`);
        }

        return parts.length
            ? `<div class="nc-card__row">${parts.join('')}</div>`
            : '';
    }

    _buildMeta(n) {
        const parts = [];
        if (n.hardware_model) {
            parts.push(`<span class="nc-chip nc-chip--meta">${this._esc(n.hardware_model)}</span>`);
        }
        if (n.role != null) {
            parts.push(`<span class="nc-chip nc-chip--meta">${this._roleName(n.role)}</span>`);
        }
        parts.push(`<span class="nc-chip nc-chip--id">!${this._esc(n.node_id)}</span>`);

        return `<div class="nc-card__row nc-card__row--meta">${parts.join('')}</div>`;
    }

    _signalBars(rssi) {
        const level = rssi > -80 ? 5 : rssi > -95 ? 4 : rssi > -110 ? 3 : rssi > -125 ? 2 : 1;
        let bars = '';
        for (let i = 1; i <= 5; i++) {
            const active = i <= level ? 'active' : '';
            bars += `<span class="nc-bar nc-bar--h${i} ${active}"></span>`;
        }
        return `<span class="nc-bars">${bars}</span>`;
    }

    _signalQuality(rssi) {
        if (rssi > -80) return { label: 'Excellent', cls: 'excellent' };
        if (rssi > -95) return { label: 'Good', cls: 'good' };
        if (rssi > -110) return { label: 'Fair', cls: 'fair' };
        return { label: 'Poor', cls: 'poor' };
    }

    _batteryIcon(pct) {
        if (pct > 75) return '&#128267;';
        if (pct > 25) return '&#128268;';
        return '&#128269;';
    }

    _roleName(role) {
        const names = {
            0: 'CLIENT', 1: 'CLIENT_MUTE', 2: 'ROUTER',
            3: 'ROUTER_CLIENT', 4: 'REPEATER', 5: 'TRACKER',
            6: 'SENSOR', 7: 'TAK', 8: 'CLIENT_HIDDEN',
            9: 'LOST_AND_FOUND', 10: 'TAK_TRACKER',
        };
        if (typeof role === 'number') return names[role] || `ROLE_${role}`;
        return String(role).toUpperCase();
    }

    _isOnline(lastHeard) {
        if (!lastHeard) return false;
        const diff = Date.now() - new Date(lastHeard).getTime();
        return diff < 15 * 60 * 1000;
    }

    _timeAgo(ts) {
        if (!ts) return '--';
        const diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
        if (diff < 60) return 'Now';
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return `${Math.floor(diff / 86400)}d ago`;
    }

    _hashColor(str) {
        let hash = 0;
        for (let i = 0; i < str.length; i++) {
            hash = str.charCodeAt(i) + ((hash << 5) - hash);
        }
        const hue = Math.abs(hash) % 360;
        return `hsl(${hue}, 55%, 45%)`;
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str || '';
        return el.innerHTML;
    }
}
