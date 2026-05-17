/**
 * Chat view renderer for interleaved sent/received messages.
 * Handles the message display area, compose bar, auto-scroll,
 * and lazy-loading of older messages.
 */
class MessagingChat {
    constructor(containerEl, onSend) {
        this._container = containerEl;
        this._onSend = onSend;
        this._conversation = null;
        this._messages = [];
        this._loading = false;
        this._allLoaded = false;
        this._lastDayKey = null;
        this._build();
    }

    clearChat() {
        this._conversation = null;
        this._messages = [];
        this._allLoaded = false;
        this._messagesEl.innerHTML = '<div class="msg-chat__empty">Select a conversation</div>';
        this._headerName.textContent = '';
        this._headerBadge.textContent = '';
    }

    setConversation(convo) {
        this._conversation = convo;
        this._messages = [];
        this._allLoaded = false;
        this._headerName.textContent = convo.node_name || convo.node_id;
        // Phase 1 #5: third protocol badge for Reticulum (cyan, matches
        // the RNS palette used in conv list + Channels card).
        let badgeText, badgeMod;
        if (convo.protocol === 'meshcore')        { badgeText = 'MC';  badgeMod = 'mc';  }
        else if (convo.protocol === 'reticulum')  { badgeText = 'RNS'; badgeMod = 'rns'; }
        else                                       { badgeText = 'MT';  badgeMod = 'mt';  }
        this._headerBadge.textContent = badgeText;
        this._headerBadge.className =
            `msg-chat__protocol-badge msg-chat__protocol-badge--${badgeMod}`;

        this._messagesEl.innerHTML = '';
        this._lastDayKey = null;
        this._input.disabled = false;
        this._sendBtn.disabled = false;
        this._input.focus();
        this._loadMessages();
    }

    addMessage(msg) {
        this._messages.push(msg);
        this._appendBubble(msg);
        this._scrollToBottom();
    }

    addOptimisticMessage(text, protocol) {
        const msg = {
            id: Date.now(),
            direction: 'sent',
            text: text,
            node_id: this._conversation?.node_id || '',
            node_name: '',
            protocol: protocol || this._conversation?.protocol || 'meshtastic',
            channel: 0,
            timestamp: new Date().toISOString(),
            status: 'sending...',
            packet_id: '',
        };
        this.addMessage(msg);
        return msg;
    }

    updateMessageStatus(tempId, status, packetId) {
        const bubble = this._messagesEl.querySelector(`[data-msg-id="${tempId}"]`);
        if (bubble) {
            const meta = bubble.querySelector('.msg-bubble__meta');
            if (meta) {
                const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                meta.textContent = `${time} · ${status}`;
            }
        }
    }

    clear() {
        this._conversation = null;
        this._messages = [];
        this._messagesEl.innerHTML = '';
        this._headerName.textContent = 'Select a conversation';
        this._headerBadge.textContent = '';
        this._headerBadge.className = 'msg-chat__protocol-badge';
        this._input.disabled = true;
        this._sendBtn.disabled = true;
    }

    async _loadMessages() {
        if (!this._conversation || this._loading) return;
        this._loading = true;

        try {
            // Phase 1 #5: route Reticulum conversations through
            // /api/reticulum/inbox and filter to this peer. RNS
            // doesn't have a per-conversation endpoint -- the inbox
            // is one flat array with peer_hash on each message --
            // so the filter lives client-side. Same render path as
            // MT/MC once normalized to the bubble shape.
            if (this._conversation.protocol === 'reticulum') {
                await this._loadRnsMessages();
                this._allLoaded = true;  // no pagination for RNS yet
                return;
            }

            const nodeId = encodeURIComponent(this._conversation.node_id);
            const res = await fetch(`/api/messages/conversation/${nodeId}?limit=50`);
            const messages = await res.json();
            this._messages = messages;

            this._messagesEl.innerHTML = '';
            if (messages.length === 0) {
                this._messagesEl.innerHTML = '<div class="msg-chat__empty">No messages yet. Say hello!</div>';
            } else {
                messages.forEach(msg => this._appendBubble(msg));
                this._scrollToBottom();
            }

            if (messages.length < 50) this._allLoaded = true;

            await fetch(`/api/messages/conversation/${nodeId}/read`, { method: 'POST' });
        } catch (e) {
            console.error('Failed to load messages:', e);
        } finally {
            this._loading = false;
        }
    }

    async _loadRnsMessages() {
        // Fetch the merged inbox + sent log, filter to this peer,
        // sort chronologically, normalize to the bubble shape the
        // _appendBubble renderer expects (direction "sent"/"received",
        // text content, timestamp ISO).
        const peer = (this._conversation.node_id || '').toLowerCase();
        const res = await fetch('/api/reticulum/inbox?limit=500');
        if (!res.ok) {
            this._messagesEl.innerHTML = '<div class="msg-chat__empty">'
                + 'Inbox unavailable. Is rnsd/lxmd running?</div>';
            return;
        }
        const data = await res.json();
        const all = data.messages || [];
        const filtered = all
            .filter(m => (m.peer_hash || '').toLowerCase() === peer)
            .sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0))
            .map(m => ({
                id:         m.hash || `${m.iso}-${m.direction}`,
                direction:  m.direction === 'out' ? 'sent' : 'received',
                text:       m.title ? `${m.title}\n${m.content || ''}` : (m.content || ''),
                node_id:    m.peer_hash,
                node_name:  m.peer_display_name || '',
                protocol:   'reticulum',
                timestamp:  m.iso || new Date(
                    (m.timestamp || 0) * 1000
                ).toISOString(),
                status:     '',
            }));
        this._messages = filtered;
        this._messagesEl.innerHTML = '';
        this._lastDayKey = null;
        if (filtered.length === 0) {
            this._messagesEl.innerHTML =
                '<div class="msg-chat__empty">No messages with this peer yet. '
                + 'Type below to send the first one.</div>';
        } else {
            filtered.forEach(msg => this._appendBubble(msg));
            this._scrollToBottom();
        }
    }

    _appendBubble(msg) {
        const empty = this._messagesEl.querySelector('.msg-chat__empty');
        if (empty) empty.remove();

        this._insertDaySeparator(msg.timestamp);

        const bubble = document.createElement('div');
        bubble.className = `msg-bubble msg-bubble--${msg.direction}`;
        bubble.dataset.msgId = msg.id;
        if (msg.packet_id) bubble.dataset.pktId = msg.packet_id;

        const time = msg.timestamp
            ? new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : '';
        const statusText = msg.status && msg.status !== 'delivered' && msg.status !== 'read'
            ? ` · ${msg.status}` : '';

        let senderHtml = '';
        if (msg.direction === 'received') {
            let name = msg.node_name || msg.node_id || '';
            if (name.startsWith('broadcast:')) name = '';
            if (name) senderHtml = `<div class="msg-bubble__sender">${this._esc(name)}</div>`;
        }

        const signalHtml = this._buildSignalHtml(msg);

        bubble.innerHTML = `
            ${senderHtml}
            <div class="msg-bubble__text">${this._esc(msg.text)}</div>
            <div class="msg-bubble__meta">${time}${statusText}${signalHtml}</div>
        `;

        this._messagesEl.appendChild(bubble);
    }

    _scrollToBottom() {
        requestAnimationFrame(() => {
            this._messagesEl.scrollTop = this._messagesEl.scrollHeight;
        });
    }

    _handleSend() {
        const text = this._input.value.trim();
        if (!text || !this._conversation) return;

        this._input.value = '';
        this._onSend(text, this._conversation);
    }

    _build() {
        this._container.innerHTML = `
            <div class="msg-chat__header">
                <span class="msg-chat__name">Select a conversation</span>
                <span class="msg-chat__protocol-badge"></span>
            </div>
            <div class="msg-chat__messages"></div>
            <div class="msg-compose">
                <input class="msg-compose__input" placeholder="Type a message..." disabled maxlength="228" />
                <button class="msg-compose__send" disabled>Send</button>
            </div>
        `;

        this._headerName = this._container.querySelector('.msg-chat__name');
        this._headerBadge = this._container.querySelector('.msg-chat__protocol-badge');
        this._messagesEl = this._container.querySelector('.msg-chat__messages');
        this._input = this._container.querySelector('.msg-compose__input');
        this._sendBtn = this._container.querySelector('.msg-compose__send');

        this._sendBtn.addEventListener('click', () => this._handleSend());
        this._input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this._handleSend();
            }
        });

        this._messagesEl.addEventListener('scroll', () => {
            if (this._messagesEl.scrollTop === 0 && !this._allLoaded) {
                this._loadOlderMessages();
            }
        });
    }

    async _loadOlderMessages() {
        if (!this._conversation || this._loading || this._allLoaded) return;
        if (this._messages.length === 0) return;
        this._loading = true;

        try {
            const oldest = this._messages[0];
            const nodeId = encodeURIComponent(this._conversation.node_id);
            const res = await fetch(
                `/api/messages/conversation/${nodeId}?limit=50&before=${oldest.timestamp}`
            );
            const older = await res.json();
            if (older.length === 0) {
                this._allLoaded = true;
                return;
            }

            const scrollBefore = this._messagesEl.scrollHeight;
            const frag = document.createDocumentFragment();
            let prevKey = null;
            older.forEach(msg => {
                const key = msg.timestamp ? this._dayKey(msg.timestamp) : null;
                if (key && key !== prevKey) {
                    const sep = this._buildDaySeparatorEl(msg.timestamp);
                    frag.appendChild(sep);
                    prevKey = key;
                }
                frag.appendChild(this._buildBubbleEl(msg));
            });

            const firstExistingKey = this._messages[0]?.timestamp
                ? this._dayKey(this._messages[0].timestamp) : null;
            if (prevKey && prevKey === firstExistingKey) {
                const existingSep = this._messagesEl.querySelector('.msg-day-separator');
                if (existingSep) existingSep.remove();
            }

            this._messagesEl.prepend(frag);
            this._messages = [...older, ...this._messages];

            const scrollAfter = this._messagesEl.scrollHeight;
            this._messagesEl.scrollTop = scrollAfter - scrollBefore;
        } catch (e) {
            console.error('Failed to load older messages:', e);
        } finally {
            this._loading = false;
        }
    }

    _buildBubbleEl(msg) {
        const bubble = document.createElement('div');
        bubble.className = `msg-bubble msg-bubble--${msg.direction}`;
        bubble.dataset.msgId = msg.id;
        if (msg.packet_id) bubble.dataset.pktId = msg.packet_id;
        const time = msg.timestamp
            ? new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : '';
        const signalHtml = this._buildSignalHtml(msg);

        let senderHtml = '';
        if (msg.direction === 'received') {
            let name = msg.node_name || msg.node_id || '';
            if (name.startsWith('broadcast:')) name = '';
            if (name) senderHtml = `<div class="msg-bubble__sender">${this._esc(name)}</div>`;
        }

        bubble.innerHTML = `
            ${senderHtml}
            <div class="msg-bubble__text">${this._esc(msg.text)}</div>
            <div class="msg-bubble__meta">${time}${signalHtml}</div>
        `;
        return bubble;
    }

    updateBubbleSignal(packetId, rssi, snr, rxCount) {
        const bubble = this._messagesEl.querySelector(`[data-pkt-id="${packetId}"]`);
        if (!bubble) return;

        const meta = bubble.querySelector('.msg-bubble__meta');
        if (!meta) return;

        const sig = meta.querySelector('.msg-signal');
        if (sig) sig.remove();
        const rx = meta.querySelector('.msg-rx-count');
        if (rx) rx.remove();

        const fakeMsg = { direction: 'received', rssi, snr, rx_count: rxCount };
        meta.insertAdjacentHTML('beforeend', this._buildSignalHtml(fakeMsg));
    }

    _buildSignalHtml(msg) {
        if (msg.direction === 'sent' || msg.rssi == null) return '';

        const rssi = msg.rssi;
        const snr = msg.snr;
        const level = rssi > -80 ? 5 : rssi > -95 ? 4 : rssi > -110 ? 3 : rssi > -125 ? 2 : 1;
        const cls = level >= 4 ? 'excellent' : level === 3 ? 'good' : level === 2 ? 'fair' : 'poor';

        let bars = '';
        for (let i = 1; i <= 5; i++) {
            const active = i <= level ? 'active' : '';
            bars += `<span class="sig-bar sig-bar--h${i} ${active}"></span>`;
        }

        const snrStr = snr != null ? ` · ${snr.toFixed(1)} dB` : '';
        const rxStr = (msg.rx_count || 1) > 1
            ? `<span class="msg-rx-count" title="Received via ${msg.rx_count} RF paths">×${msg.rx_count}</span>`
            : '';
        return `<span class="msg-signal msg-signal--${cls}">${bars}<span class="msg-signal__val">${rssi.toFixed(1)}${snrStr}</span></span>${rxStr}`;
    }

    _insertDaySeparator(ts) {
        if (!ts) return;
        const key = this._dayKey(ts);
        if (key === this._lastDayKey) return;
        this._lastDayKey = key;

        const d = new Date(ts);
        const now = new Date();
        const sameDay = d.getFullYear() === now.getFullYear()
            && d.getMonth() === now.getMonth()
            && d.getDate() === now.getDate();

        let label;
        if (sameDay) {
            label = 'Today';
        } else {
            const yesterday = new Date(now);
            yesterday.setDate(yesterday.getDate() - 1);
            const sameYesterday = d.getFullYear() === yesterday.getFullYear()
                && d.getMonth() === yesterday.getMonth()
                && d.getDate() === yesterday.getDate();
            if (sameYesterday) {
                label = 'Yesterday';
            } else {
                label = d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
            }
        }

        const div = document.createElement('div');
        div.className = 'msg-day-separator';
        div.textContent = label;
        this._messagesEl.appendChild(div);
    }

    _buildDaySeparatorEl(ts) {
        const d = new Date(ts);
        const now = new Date();
        const sameDay = d.getFullYear() === now.getFullYear()
            && d.getMonth() === now.getMonth()
            && d.getDate() === now.getDate();

        let label;
        if (sameDay) {
            label = 'Today';
        } else {
            const yesterday = new Date(now);
            yesterday.setDate(yesterday.getDate() - 1);
            const sameYesterday = d.getFullYear() === yesterday.getFullYear()
                && d.getMonth() === yesterday.getMonth()
                && d.getDate() === yesterday.getDate();
            label = sameYesterday
                ? 'Yesterday'
                : d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
        }

        const div = document.createElement('div');
        div.className = 'msg-day-separator';
        div.textContent = label;
        return div;
    }

    _dayKey(ts) {
        const d = new Date(ts);
        return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str || '';
        return el.innerHTML;
    }
}
