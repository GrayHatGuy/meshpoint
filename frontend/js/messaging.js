/**
 * Main messaging panel controller for the local Meshpoint dashboard.
 * Manages the two-column layout (sidebar + chat), tab switching,
 * protocol filtering, monitor mode, WebSocket event routing, and
 * send orchestration.
 */
class MessagingPanel {
    constructor() {
        this._initialized = false;
        this._contacts = null;
        this._chat = null;
        this._activeConvo = null;
        this._unreadTotal = 0;
        this._monitorMode = false;
        this._txStatus = null;
    }

    init() {
        if (this._initialized) return;
        this._initialized = true;

        const panel = document.getElementById('messaging-panel');
        if (!panel) return;

        panel.innerHTML = `
            <div id="msg-tx-banner" class="msg-tx-banner" style="display:none"></div>
            <div class="messaging">
                <div class="msg-sidebar">
                    <div class="msg-sidebar__header">
                        <span class="msg-sidebar__title">Messages</span>
                        <div class="msg-sidebar__actions">
                            <button class="msg-sidebar__monitor-btn" id="msg-monitor-btn" title="Monitor: show overheard DMs">&#x1F441;</button>
                            <button class="msg-sidebar__new-btn" id="msg-new-btn">+ New</button>
                        </div>
                    </div>
                    <div class="msg-protocol-toggle">
                        <button class="msg-protocol-toggle__btn msg-protocol-toggle__btn--active" data-filter="all">All</button>
                        <button class="msg-protocol-toggle__btn" data-filter="meshtastic">MT</button>
                        <button class="msg-protocol-toggle__btn" data-filter="meshcore">MC</button>
                        <button class="msg-protocol-toggle__btn" data-filter="reticulum">RNS</button>
                    </div>
                    <div class="msg-sidebar__list" id="msg-convo-list"></div>
                </div>
                <div class="msg-chat" id="msg-chat-area"></div>
            </div>
        `;

        const listEl = document.getElementById('msg-convo-list');
        const chatEl = document.getElementById('msg-chat-area');

        this._contacts = new MessagingContacts(listEl, (convo) => this._onConversationSelected(convo));
        this._chat = new MessagingChat(chatEl, (text, convo) => this._onSendMessage(text, convo));

        document.getElementById('msg-new-btn').addEventListener('click', () => {
            this._contacts.openContactPicker();
        });

        document.getElementById('msg-monitor-btn').addEventListener('click', () => {
            this._monitorMode = !this._monitorMode;
            const btn = document.getElementById('msg-monitor-btn');
            btn.classList.toggle('msg-sidebar__monitor-btn--active', this._monitorMode);
            btn.title = this._monitorMode ? 'Monitor ON: showing overheard DMs' : 'Monitor: show overheard DMs';
            this._contacts.load(this._monitorMode);
        });

        panel.querySelectorAll('.msg-protocol-toggle__btn').forEach(btn => {
            btn.addEventListener('click', () => {
                panel.querySelectorAll('.msg-protocol-toggle__btn').forEach(b => b.classList.remove('msg-protocol-toggle__btn--active'));
                btn.classList.add('msg-protocol-toggle__btn--active');
                this._contacts.setFilter(btn.dataset.filter);
            });
        });

        this._setupWebSocket();
        this._contacts.load(this._monitorMode);
        this._loadStatus();
    }

    onActivated() {
        if (!this._initialized) this.init();
        this._contacts.load(this._monitorMode);
    }

    openConversation(convo) {
        if (!this._initialized) this.init();
        this._onConversationSelected(convo);
    }

    _onConversationSelected(convo) {
        if (!convo) {
            this._activeConvo = null;
            this._chat.clearChat();
            return;
        }
        this._activeConvo = convo;
        this._chat.setConversation(convo);
        this._contacts.setActive(convo.node_id);
    }

    async _onSendMessage(text, convo) {
        // Phase 1 #5: Reticulum sends route through the LXMF endpoint
        // instead of /api/messages/send. The shape is different
        // (destination_hash + content) and the optimistic+ack flow
        // is simpler -- lxmsendmsg either succeeds or 502s, no
        // separate ack callback. After success we trigger a contacts
        // refresh so the conversation list picks up the new entry.
        if (convo.protocol === 'reticulum') {
            await this._sendRnsMessage(text, convo);
            return;
        }

        const isBroadcast = convo.is_broadcast || (convo.node_id || '').startsWith('broadcast:');
        const destination = isBroadcast ? 'broadcast' : convo.node_id;

        const tempMsg = this._chat.addOptimisticMessage(text, convo.protocol);

        try {
            const res = await fetch('/api/messages/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: text,
                    destination: destination,
                    protocol: convo.protocol || 'meshtastic',
                    channel: convo.channel || 0,
                }),
            });

            if (!res.ok) {
                const errBody = await res.json().catch(() => ({}));
                const reason = errBody.detail || errBody.error || `HTTP ${res.status}`;
                this._chat.updateMessageStatus(tempMsg.id, `failed: ${reason}`, '');
                return;
            }

            const result = await res.json();
            if (result.success) {
                this._chat.updateMessageStatus(tempMsg.id, 'sent', result.packet_id);
                this._contacts.addOrUpdateConversation({
                    node_id: convo.node_id,
                    node_name: convo.node_name,
                    protocol: convo.protocol,
                    text: text,
                    direction: 'sent',
                    timestamp: new Date().toISOString(),
                });
            } else {
                const reason = result.error || 'unknown error';
                this._chat.updateMessageStatus(tempMsg.id, `failed: ${reason}`, '');
            }
        } catch (e) {
            console.error('Send failed:', e);
            this._chat.updateMessageStatus(tempMsg.id, 'network error', '');
        }
    }

    async _sendRnsMessage(text, convo) {
        // Mirror the optimistic-bubble pattern used by MT/MC sends.
        const tempMsg = this._chat.addOptimisticMessage(text, 'reticulum');
        try {
            const res = await fetch('/api/reticulum/send', {
                method:  'POST',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({
                    destination_hash: convo.node_id,
                    content:          text,
                }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                this._chat.updateMessageStatus(
                    tempMsg.id,
                    `failed: ${err.detail || `HTTP ${res.status}`}`, '',
                );
                return;
            }
            this._chat.updateMessageStatus(tempMsg.id, 'sent', '');
            // Bump the conv list locally so the new message shows in
            // the sidebar before the next WS-driven refresh tick.
            this._contacts.addOrUpdateConversation({
                node_id:   convo.node_id,
                node_name: convo.node_name,
                protocol:  'reticulum',
                text:      text,
                direction: 'sent',
                timestamp: new Date().toISOString(),
            });
        } catch (e) {
            this._chat.updateMessageStatus(
                tempMsg.id, `network error: ${e.message}`, '',
            );
        }
    }

    _setupWebSocket() {
        window.concentratorWS.on('message_received', (data) => {
            const isOverheard = data.direction === 'overheard';

            if (isOverheard && !this._monitorMode) {
                return;
            }

            if (this._activeConvo && data.node_id === this._activeConvo.node_id) {
                const chParts = (data.node_id || '').split(':');
                const chIdx = chParts.length >= 3 ? parseInt(chParts[2], 10) || 0 : 0;
                const msg = {
                    id: Date.now(),
                    direction: data.direction || 'received',
                    text: data.text,
                    node_id: data.node_id,
                    node_name: data.node_name || '',
                    protocol: data.protocol || 'meshtastic',
                    channel: chIdx,
                    timestamp: new Date().toISOString(),
                    status: 'delivered',
                    packet_id: data.packet_id || '',
                    source_id: data.source_id || '',
                    destination_id: data.destination_id || '',
                };
                if (data.rssi != null) msg.rssi = data.rssi;
                if (data.snr != null) msg.snr = data.snr;
                this._chat.addMessage(msg);
            }
            this._contacts.addOrUpdateConversation(data);
            if (!isOverheard) this._updateUnreadBadge();
        });

        window.concentratorWS.on('message_updated', (data) => {
            if (this._activeConvo && data.node_id === this._activeConvo.node_id) {
                this._chat.updateBubbleSignal(
                    data.packet_id, data.rssi, data.snr, data.rx_count
                );
            }
        });

        window.concentratorWS.on('message_sent', (data) => {
            this._contacts.addOrUpdateConversation({
                ...data,
                direction: 'sent',
            });
        });

        // Phase 1 #5: refresh RNS conversations when the sidecar
        // republishes inbox/peers/contacts JSON. Inexpensive --
        // .load() rebatches all three (MT convos, channels, RNS).
        // If the currently-open thread is RNS, also reload its
        // bubbles so a freshly-arrived message appears live.
        window.concentratorWS.on('lxmf_inbox_changed', () => {
            this._contacts.load(this._monitorMode);
            if (this._activeConvo
                    && this._activeConvo.protocol === 'reticulum') {
                this._chat.setConversation(this._activeConvo);
            }
        });
    }

    // Phase 1 #5: cross-tab entry point used by the Radio tab's
    // Channels card. Switches to the Messages tab and opens the
    // conversation for the given LXMF address. If no conversation
    // exists yet (no messages with that peer), we synthesise one
    // so the operator can start typing right away.
    openRnsConversation(peerHash, peerName) {
        const tabBtn = document.querySelector('[data-tab="messages"]');
        if (tabBtn) tabBtn.click();
        // Defer the conversation open one tick so the tab switch
        // has flushed and our contacts panel is in the DOM.
        setTimeout(() => {
            const existing = this._contacts._conversations.find(
                c => c.protocol === 'reticulum' && c.node_id === peerHash
            );
            const convo = existing || {
                node_id:        peerHash,
                node_name:      peerName || (peerHash.slice(0, 12) + '…'),
                protocol:       'reticulum',
                last_message:   '',
                last_timestamp: '',
                unread_count:   0,
                is_broadcast:   false,
            };
            this._onConversationSelected(convo);
        }, 50);
    }

    async _loadStatus() {
        try {
            const res = await fetch('/api/messages/status');
            const status = await res.json();
            this._txStatus = status;
            this._renderTxBanner(status);
        } catch (e) {
            this._renderTxBanner(null);
        }
    }

    _renderTxBanner(status) {
        const banner = document.getElementById('msg-tx-banner');
        if (!banner) return;

        if (!status) {
            banner.style.display = 'block';
            banner.className = 'msg-tx-banner msg-tx-banner--error';
            banner.innerHTML = 'TX status unavailable';
            return;
        }

        const mt = status.meshtastic || {};
        const mc = status.meshcore || {};

        if (!mt.enabled && !mc.connected) {
            banner.style.display = 'block';
            banner.className = 'msg-tx-banner msg-tx-banner--warn';
            banner.innerHTML = 'TX not configured. <a href="#" onclick="document.querySelector(\'[data-tab=radio]\').click();return false">Open Radio tab</a> to enable.';
            return;
        }

        if (mt.enabled && !mt.node_id) {
            banner.style.display = 'block';
            banner.className = 'msg-tx-banner msg-tx-banner--warn';
            banner.innerHTML = 'Node ID not set. <a href="#" onclick="document.querySelector(\'[data-tab=radio]\').click();return false">Set in Radio tab</a>.';
            return;
        }

        banner.style.display = 'none';
    }

    _updateUnreadBadge() {
        const badge = document.getElementById('msg-unread-badge');
        if (!badge) return;
        this._unreadTotal++;
        badge.textContent = this._unreadTotal;
        badge.style.display = this._unreadTotal > 0 ? 'inline-block' : 'none';
    }

    resetUnreadBadge() {
        this._unreadTotal = 0;
        const badge = document.getElementById('msg-unread-badge');
        if (badge) badge.style.display = 'none';
    }
}

window.messagingPanel = new MessagingPanel();
