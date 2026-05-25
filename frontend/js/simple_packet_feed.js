/**
 * Simple live packet feed for the local Meshpoint dashboard.
 * Renders incoming packets via WebSocket with expand-on-click.
 */
class SimplePacketFeed {
    constructor(tbodyId, maxRows) {
        this._tbody = document.getElementById(tbodyId);
        this._maxRows = maxRows || 200;
        this._count = 0;
        this._nodeByLastByte = new Map();
        this._onFocus = null;

        // Phase 1 #1: Reticulum peer enrichment map.
        // {hash_hex -> {display_name, class, is_lxmf, app_data_hex}}
        // Populated by _refreshPeerMap() on mount and on every
        // WebSocket "lxmf_inbox_changed" event. Empty {} until the
        // first fetch lands -- renderer degrades to "unknown · hash".
        this._rnsPeers = {};
        this._refreshPeerMap();
        this._wirePeerMapRefresh();
    }

    _wirePeerMapRefresh() {
        // Subscribe to the existing /ws WebSocket for live peer-map
        // updates. The sidecar broadcasts "lxmf_inbox_changed" on
        // every inbox.json / sent_log / peers.json mtime change.
        // We re-fetch the map on each event so the address book
        // applies retroactively to every visible packet row without
        // requiring a page refresh.
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        try {
            const ws = new WebSocket(`${proto}//${location.host}/ws`);
            ws.addEventListener('message', (ev) => {
                try {
                    const msg = JSON.parse(ev.data);
                    if (msg.type === 'lxmf_inbox_changed') {
                        this._refreshPeerMap();
                    }
                } catch (e) { /* ignore non-JSON frames */ }
            });
            // Reconnect on close so a meshpoint restart doesn't leave
            // the packet feed with a permanently-stale peer map.
            ws.addEventListener('close', () => {
                setTimeout(() => this._wirePeerMapRefresh(), 5000);
            });
        } catch (e) { /* no-op; 30s fetch fallback below */ }
    }

    async _refreshPeerMap() {
        try {
            const res = await fetch('/api/reticulum/peer_map');
            if (!res.ok) return;
            const data = await res.json();
            this._rnsPeers = data.peers || {};
            // Re-render existing rows so display names apply
            // retroactively (operator's intent per Phase 1 #2 spec).
            this._rerenderRnsRows();
        } catch (e) { /* no-op; map stays as last known good */ }
    }

    _rerenderRnsRows() {
        // Walk visible packet rows tagged data-protocol="reticulum"
        // and rewrite their source/dest/type cells using the freshly
        // fetched peer map. Cells we DIDN'T touch (RSSI, time, freq)
        // are left as-is.
        if (!this._tbody) return;
        this._tbody.querySelectorAll('tr[data-protocol="reticulum"]').forEach((tr) => {
            const src = tr.dataset.rnsSource || '';
            const dst = tr.dataset.rnsDest   || '';
            const typ = tr.dataset.rnsType   || 'unknown';
            const srcCell = tr.querySelector('.td-source');
            const dstCell = tr.children[3]; // Dest column index
            const typCell = tr.children[4]; // Type column index
            if (srcCell) srcCell.innerHTML = this._renderRnsCell(src, typ === 'announce');
            if (dstCell) dstCell.innerHTML = this._renderRnsCell(dst, false);
            if (typCell) typCell.textContent = typ;
        });
    }

    setOnFocus(cb) {
        this._onFocus = cb;
    }

    loadNodes(nodes) {
        this._nodeByLastByte.clear();
        for (const node of nodes) {
            const id = node.node_id;
            if (id && id.length >= 2) {
                this._nodeByLastByte.set(id.slice(-2).toLowerCase(), id);
            }
        }
    }

    addPacket(packet) {
        const tr = document.createElement('tr');
        tr.classList.add('packet-row--new');
        tr.addEventListener('animationend', () => tr.classList.remove('packet-row--new'));

        const time = packet.rx_time
            ? new Date(packet.rx_time * 1000).toLocaleTimeString()
            : packet.timestamp
                ? new Date(packet.timestamp).toLocaleTimeString()
                : new Date().toLocaleTimeString();

        const sig = packet.signal || {};
        const rawRssi = sig.rssi != null ? sig.rssi : packet.rssi;
        const rawSnr = sig.snr != null ? sig.snr : packet.snr;
        const rssiVal = rawRssi != null ? Number(rawRssi).toFixed(0) : null;
        const rssi = rssiVal != null ? rssiVal : '--';
        const snr = rawSnr != null ? `${Number(rawSnr).toFixed(1)}` : '--';
        let type = packet.packet_type || '--';
        const protocol = packet.protocol || 'meshtastic';

        // Phase 1 #1: Reticulum gets its own source/dest renderer that
        // looks up the hash in the peer map and uses display_name +
        // class badge when known. Falls back to "unknown · <short>"
        // when the classifier hasn't characterised the hash yet.
        let srcCell, destShort;
        const isRns = (protocol === 'reticulum');
        if (isRns) {
            // For ANNOUNCE packets the decoder now sets source_id =
            // dest_hash (the announcer broadcasts their own identity),
            // so the source column actually has something to look up.
            // For DATA packets source_id stays "unknown" -- the source
            // is inside the encrypted payload and unrecoverable.
            const isAnnounce = (packet.packet_type === 'nodeinfo'
                              || (packet.decoded_payload
                                  && packet.decoded_payload.packet_type === 1));
            if (isAnnounce) type = 'announce';
            srcCell = this._renderRnsCell(packet.source_id, isAnnounce);
            destShort = this._renderRnsCell(packet.destination_id, false);
        } else {
            const srcShort = this._shortId(packet.source_id);
            const relayByte = packet.relay_node || 0;
            srcCell = relayByte
                ? `${srcShort} <span class="relay-hop">↝ ${this._resolveRelay(relayByte)}</span>`
                : srcShort;
            destShort = this._shortId(packet.destination_id);
        }

        const details = this._summarize(packet);

        const hops = packet.hop_start > 0
            ? `${packet.hop_start - packet.hop_limit}/${packet.hop_start}`
            : '--';

        const typeClass = `type-${type.replace(/[^a-zA-Z0-9_-]/g, '')}`;
        const protocolClass = `protocol-${protocol}`;
        const rssiClass = this._rssiClass(rssiVal);

        const freqMhz = sig.frequency_mhz || packet.frequency_mhz;
        const freq = freqMhz ? `${Number(freqMhz).toFixed(1)}` : '--';
        const sfVal = sig.spreading_factor || packet.spreading_factor;
        const sf = sfVal ? `SF${sfVal}` : '--';

        // Phase 1 #1: stash original Reticulum hashes on the row so
        // _rerenderRnsRows() can rewrite the source/dest/type cells
        // when a fresh peer_map arrives later (retro nickname apply).
        if (isRns) {
            tr.dataset.protocol  = 'reticulum';
            tr.dataset.rnsSource = packet.source_id || '';
            tr.dataset.rnsDest   = packet.destination_id || '';
            tr.dataset.rnsType   = type;
        }

        tr.innerHTML = `
            <td>${time}</td>
            <td class="${protocolClass}">${protocol}</td>
            <td class="td-source">${srcCell}</td>
            <td>${destShort}</td>
            <td class="${typeClass}">${type}</td>
            <td class="${rssiClass}">${rssi}</td>
            <td>${snr}</td>
            <td class="td-freq">${freq}</td>
            <td class="td-sf">${sf}</td>
            <td>${hops}</td>
            <td class="packet-details-cell ${typeClass}">${this._esc(details)}</td>
        `;

        tr.addEventListener('click', () => this._toggleDetail(tr, packet));

        this._tbody.prepend(tr);
        this._count++;

        const countEl = document.getElementById('packet-count');
        if (countEl) countEl.textContent = this._count;

        while (this._tbody.children.length > this._maxRows * 2) {
            this._tbody.removeChild(this._tbody.lastChild);
        }
    }

    _toggleDetail(tr, packet) {
        const next = tr.nextElementSibling;
        if (next && next.classList.contains('packet-detail-row')) {
            next.remove();
            if (this._onFocus) this._onFocus(null);
            return;
        }

        const prev = this._tbody.querySelector('.packet-detail-row');
        if (prev) prev.remove();

        if (this._onFocus) this._onFocus(packet.source_id);

        const detailTr = document.createElement('tr');
        detailTr.classList.add('packet-detail-row');
        const td = document.createElement('td');
        td.colSpan = 11;


        const payload = packet.decoded_payload;
        if (payload && typeof payload === 'object') {
            td.textContent = JSON.stringify(payload, null, 2);
        } else {
            td.textContent = `Source: ${packet.source_id || '--'}\nType: ${packet.packet_type || '--'}\nRSSI: ${packet.rssi || '--'} dBm\nSNR: ${packet.snr || '--'} dB`;
        }

        detailTr.appendChild(td);
        tr.after(detailTr);
    }

    _summarize(packet) {
        const p = packet.decoded_payload;
        if (!p) return '--';

        switch (packet.packet_type) {
            case 'text': return p.text || '--';
            case 'position': {
                const parts = [];
                if (p.latitude != null) parts.push(`${p.latitude.toFixed(4)}`);
                if (p.longitude != null) parts.push(`${p.longitude.toFixed(4)}`);
                if (p.altitude != null) parts.push(`alt ${p.altitude}m`);
                return parts.join(', ') || '--';
            }
            case 'nodeinfo':
                return [p.long_name, p.short_name, p.hw_model].filter(Boolean).join(' ') || '--';
            case 'telemetry': {
                const parts = [];
                if (p.battery_level != null) parts.push(`batt=${p.battery_level}%`);
                if (p.voltage != null) parts.push(`${Number(p.voltage).toFixed(1)}V`);
                if (p.temperature != null) parts.push(`${Number(p.temperature).toFixed(0)}°C`);
                return parts.join(' ') || '--';
            }
            default: return '--';
        }
    }

    _rssiClass(val) {
        if (val == null) return '';
        const n = Number(val);
        if (n >= -90) return 'rssi-good';
        if (n >= -110) return 'rssi-mid';
        return 'rssi-bad';
    }

    _resolveRelay(relayByte) {
        const key = relayByte.toString(16).padStart(2, '0');
        const fullId = this._nodeByLastByte.get(key);
        return fullId ? this._shortId(fullId) : `!${key}`;
    }

    _shortId(id) {
        if (!id) return '--';
        if (id === 'ffffffff' || id === 'ffff') return 'BCAST';
        return id.length > 6 ? `!${id.slice(-4)}` : id;
    }

    _renderRnsCell(rawId, isAnnounceSource) {
        // Reticulum-specific source/dest renderer.
        //
        // Inputs:
        //   rawId             -- packet.source_id or .destination_id.
        //                        For DATA packets source_id is the
        //                        literal string "unknown" (not a hex
        //                        hash) -- we never try to look it up.
        //   isAnnounceSource  -- true when rendering the source cell
        //                        of an ANNOUNCE packet. In that case
        //                        the source IS the announcer's own
        //                        destination, so we recolour the
        //                        class badge slightly to signal it.
        //
        // Substitution per the operator-facing "unknown" semantic
        // agreed in Phase 1 #2: rows where the peer is NOT in the
        // address book show "unknown · <hash-short>". Rows where the
        // peer IS in the book show the display_name plus a
        // color-coded class badge (lxmf / relay / propagation / ...).
        if (!rawId || rawId === 'unknown') {
            return '<span class="rns-unknown">unknown</span>';
        }
        // Strip any leading header byte the decoder prepended -- the
        // canonical LXMF address is the LAST 32 hex chars (16 bytes).
        const canonical = rawId.length > 32 ? rawId.slice(-32) : rawId;
        const meta = this._rnsPeers[canonical] || this._rnsPeers[rawId] || null;
        const short = canonical.length >= 8 ? canonical.slice(0, 8) + '…' : canonical;
        if (meta && meta.display_name) {
            const cls = meta.class || 'unknown';
            return `${this._esc(meta.display_name)}`
                 + ` <span class="rns-class rns-class--${cls}">${cls}</span>`;
        }
        if (meta && meta.class && meta.class !== 'unknown') {
            // No display_name but classified (relay / propagation /
            // transport) -- show the class badge so operators know
            // the hash represents infrastructure, not a chat peer.
            return `<span class="rns-unknown">unknown</span>`
                 + ` <span class="rns-class__short">·</span>`
                 + ` <span class="rns-class__hash">${this._esc(short)}</span>`
                 + ` <span class="rns-class rns-class--${meta.class}">${meta.class}</span>`;
        }
        return `<span class="rns-unknown">unknown</span>`
             + ` <span class="rns-class__short">·</span>`
             + ` <span class="rns-class__hash">${this._esc(short)}</span>`;
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str;
        return el.innerHTML;
    }
}
