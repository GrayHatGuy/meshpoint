/**
 * Radio tab - Reticulum Messages card (Phase 2 #3).
 *
 * Two-pane LXMF messaging UI:
 *   - Left:  conversation list, grouped by peer hash, sorted by
 *            most-recent activity. Each row shows the peer hash
 *            (truncated), the latest message preview, and time.
 *   - Right: thread for the selected conversation. In/out bubbles
 *            tagged by direction. Compose box at the bottom.
 *
 * Live updates: subscribes to the existing /ws WebSocket and
 * listens for "lxmf_inbox_changed" events broadcast by the server
 * watcher. On every such event, re-fetch the inbox. Fallback: 30s
 * polling in case the WebSocket disconnects.
 *
 * Backend contract:
 *   GET  /api/reticulum/inbox        -> {messages: [...], count, generated_at}
 *     Each message: {direction: "in"|"out", hash, peer_hash,
 *                    title, content, timestamp, iso}
 *   POST /api/reticulum/send         -> {sent: true, ...}
 *     Body: {destination_hash, content, title?}
 *
 * The card stays in a quiet "no messages yet" state if the backend
 * returns nothing -- it does NOT show a "stack not detected" error
 * because the Identity card already covers that surface; doubling
 * up the offline messaging is noisy.
 */
class RnsMessagesCard {
    constructor(api) {
        this._api = api;
        this._root = null;
        this._messages = [];           // flat list from /api/reticulum/inbox
        this._conversations = new Map(); // peer_hash -> {peer_hash, latest, count, msgs[]}
        this._selectedPeer = null;     // currently-open conversation
        this._localAddr = null;        // our own LXMF address (for "to self" rendering)
        this._refreshTimer = null;
        this._ws = null;
        this._composeTo = '';          // for the "new conversation" form
    }

    mount(rootEl) {
        this._root = rootEl;
        rootEl.classList.add('r-card');
        rootEl.innerHTML = `
            <div class="r-card__header">
                <h3 class="r-card__title">Messages</h3>
                <span class="r-badge r-badge--mono" id="rns-msgs-status">--</span>
            </div>
            <div class="rns-msgs">
                <aside class="rns-msgs__list" id="rns-msgs-list">
                    <div class="rns-msgs__list-head">
                        <span class="rns-msgs__list-title">Conversations</span>
                        <button class="r-btn r-btn--secondary rns-msgs__newbtn"
                                id="rns-msgs-new" title="Start a new conversation">+ New</button>
                    </div>
                    <div class="rns-msgs__convos" id="rns-msgs-convos">
                        <div class="rns-msgs__empty">Loading...</div>
                    </div>
                </aside>
                <section class="rns-msgs__thread" id="rns-msgs-thread">
                    <div class="rns-msgs__placeholder">
                        Select a conversation, or click <b>+ New</b> to send a
                        message to a peer hash.
                    </div>
                </section>
            </div>
            <p class="r-hint" id="rns-msgs-hint">
                LXMF messages. Each peer is identified by its 32-char hash --
                find one in the Channels card above.
            </p>
        `;
        this._wire();
    }

    render(_config) {
        this._fetchLocalAddress();
        this._fetchInbox();
        // Periodic refresh as a WebSocket-disconnect fallback.
        if (this._refreshTimer) clearInterval(this._refreshTimer);
        this._refreshTimer = setInterval(() => this._fetchInbox(), 30_000);
        this._connectWebSocket();
    }

    _wire() {
        this._root.querySelector('#rns-msgs-new').addEventListener(
            'click', () => this._openNewConversationForm(),
        );
    }

    // ── Data plumbing ───────────────────────────────────────────

    async _fetchLocalAddress() {
        try {
            const res = await fetch('/api/reticulum/identity');
            if (!res.ok) return;
            const data = await res.json();
            this._localAddr = data.address || null;
        } catch (e) {
            // No-op -- worst case we render peer hashes without a "to self" tag.
        }
    }

    async _fetchInbox() {
        try {
            const res = await fetch('/api/reticulum/inbox?limit=500');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this._messages = data.messages || [];
            this._rebuildConversations();
            this._renderConversations();
            if (this._selectedPeer) {
                this._renderThread(this._selectedPeer);
            }
            this._setStatus('LIVE');
        } catch (e) {
            this._setStatus('OFFLINE');
            const convos = this._root.querySelector('#rns-msgs-convos');
            if (convos && this._conversations.size === 0) {
                convos.innerHTML = `<div class="rns-msgs__empty">
                    Inbox unavailable (${this._esc(e.message)}).
                </div>`;
            }
        }
    }

    _rebuildConversations() {
        // Group messages by peer_hash. peer_hash is always "the other side":
        // for direction=in it's the sender, for direction=out it's the recipient.
        // Self-messages (sent to our own hash, then echoed back via the radio)
        // collapse into a single self-thread which is useful for testing.
        //
        // Phase 1 #2: each message also carries peer_display_name +
        // peer_class from the backend's enrichment. Per-conversation, we
        // pick the FIRST non-null display_name we see (they should all
        // match since they're for the same hash, but messages from
        // before the classifier ran will have nulls -- prefer the named
        // one). Stored on the conv object so renderers don't have to
        // walk msgs[] every time.
        this._conversations.clear();
        for (const m of this._messages) {
            const peer = m.peer_hash || '';
            if (!peer) continue;
            if (!this._conversations.has(peer)) {
                this._conversations.set(peer, {
                    peer_hash: peer, msgs: [], latest: null,
                    display_name: null, peer_class: null,
                });
            }
            const conv = this._conversations.get(peer);
            conv.msgs.push(m);
            if (!conv.latest || (m.timestamp || 0) > (conv.latest.timestamp || 0)) {
                conv.latest = m;
            }
            if (!conv.display_name && m.peer_display_name) {
                conv.display_name = m.peer_display_name;
            }
            if (!conv.peer_class && m.peer_class) {
                conv.peer_class = m.peer_class;
            }
        }
        // Sort each conversation's messages oldest-first for thread display.
        for (const conv of this._conversations.values()) {
            conv.msgs.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
        }
    }

    // ── Conversation list ───────────────────────────────────────

    _renderConversations() {
        const convos = this._root.querySelector('#rns-msgs-convos');
        if (this._conversations.size === 0) {
            convos.innerHTML = `<div class="rns-msgs__empty">
                No messages yet. Send one with <b>+ New</b>.
            </div>`;
            return;
        }
        const sorted = [...this._conversations.values()].sort(
            (a, b) => (b.latest.timestamp || 0) - (a.latest.timestamp || 0),
        );
        convos.innerHTML = sorted.map((conv) => this._convoRow(conv)).join('');
        // Click handlers -- delegated by querying after innerHTML.
        convos.querySelectorAll('.rns-msgs__convo').forEach((row) => {
            row.addEventListener('click', () => {
                this._selectedPeer = row.dataset.peer;
                this._renderConversations();          // re-highlight
                this._renderThread(this._selectedPeer);
            });
        });
    }

    _convoRow(conv) {
        const peer = conv.peer_hash;
        const selected = (peer === this._selectedPeer) ? ' rns-msgs__convo--sel' : '';
        const isSelf = (peer === this._localAddr);
        const preview = (conv.latest.content || '').slice(0, 60);
        const dir = conv.latest.direction === 'out' ? '↑' : '↓';
        const peerShort = peer.slice(0, 12) + '…';

        // Phase 1 #2: prefer the auto-discovered display_name when we
        // have one. Always tack on the truncated hash for disambiguation
        // because two devices can share a name (e.g., two default "4w4"
        // installs). Self always wins regardless of announce data.
        let tag;
        if (isSelf) {
            tag = '<em>(self)</em>';
        } else if (conv.display_name) {
            tag = `${this._esc(conv.display_name)} <span class="rns-msgs__convo-hash">${this._esc(peerShort)}</span>`;
        } else {
            tag = this._esc(peerShort);
        }
        return `
            <div class="rns-msgs__convo${selected}" data-peer="${this._esc(peer)}">
                <div class="rns-msgs__convo-head">
                    <span class="rns-msgs__convo-peer">${tag}</span>
                    <span class="rns-msgs__convo-time">${this._fmtTime(conv.latest.iso)}</span>
                </div>
                <div class="rns-msgs__convo-preview">
                    <span class="rns-msgs__convo-dir">${dir}</span>
                    ${this._esc(preview)}
                </div>
            </div>
        `;
    }

    // ── Thread view ─────────────────────────────────────────────

    _renderThread(peerHash) {
        const thread = this._root.querySelector('#rns-msgs-thread');
        const conv = this._conversations.get(peerHash);
        if (!conv) {
            thread.innerHTML = `<div class="rns-msgs__placeholder">
                No messages with this peer yet.
            </div>`;
            return;
        }
        const isSelf = (peerHash === this._localAddr);
        // Thread header shows BOTH name + hash when we have a name,
        // so the operator can verify they're messaging the right
        // device when nicknames collide. Self always renders as (self).
        let peerLabel;
        if (isSelf) {
            peerLabel = '(self)';
        } else if (conv.display_name) {
            peerLabel = `${conv.display_name}  <${peerHash}>`;
        } else {
            peerLabel = peerHash;
        }
        const classBadge = (conv.peer_class && conv.peer_class !== 'unknown')
            ? `<span class="rns-class rns-class--${conv.peer_class}">${conv.peer_class}</span>`
            : '';
        thread.innerHTML = `
            <div class="rns-msgs__thread-head">
                <div class="rns-msgs__thread-peer" title="${this._esc(peerHash)}">
                    To: <span class="rns-msgs__thread-hash">${this._esc(peerLabel)}</span>
                    ${classBadge}
                </div>
                <span class="rns-msgs__thread-count">${conv.msgs.length} msg(s)</span>
            </div>
            <div class="rns-msgs__bubbles" id="rns-msgs-bubbles">
                ${conv.msgs.map((m) => this._bubble(m)).join('')}
            </div>
            <div class="rns-msgs__compose">
                <input class="r-input" type="text"
                       id="rns-msgs-title-${this._slug(peerHash)}"
                       placeholder="(optional title)" />
                <textarea class="r-input rns-msgs__body"
                          id="rns-msgs-body-${this._slug(peerHash)}"
                          rows="2" placeholder="Type a message..."></textarea>
                <button class="r-btn r-btn--primary rns-msgs__send"
                        id="rns-msgs-send-${this._slug(peerHash)}">Send</button>
            </div>
        `;
        // Scroll bubbles to the bottom (newest at the bottom in thread view).
        const bubbles = thread.querySelector('#rns-msgs-bubbles');
        if (bubbles) bubbles.scrollTop = bubbles.scrollHeight;

        const sendBtn = thread.querySelector(`#rns-msgs-send-${this._slug(peerHash)}`);
        sendBtn.addEventListener('click', () => this._send(peerHash));
    }

    _bubble(m) {
        const cls = m.direction === 'out' ? 'rns-msgs__bubble--out' : 'rns-msgs__bubble--in';
        const title = m.title ? `<div class="rns-msgs__bubble-title">${this._esc(m.title)}</div>` : '';
        return `
            <div class="rns-msgs__bubble ${cls}">
                ${title}
                <div class="rns-msgs__bubble-body">${this._esc(m.content)}</div>
                <div class="rns-msgs__bubble-meta">${this._fmtTime(m.iso)}</div>
            </div>
        `;
    }

    // ── Send + new-conversation ─────────────────────────────────

    _openNewConversationForm() {
        // Inline a "to:" + compose form into the thread pane. Once the
        // user sends, the new conversation appears in the left list
        // (after the inbox re-fetch via WebSocket) and gets selected.
        const thread = this._root.querySelector('#rns-msgs-thread');
        thread.innerHTML = `
            <div class="rns-msgs__thread-head">
                <div class="rns-msgs__thread-peer">New message</div>
            </div>
            <div class="rns-msgs__newform">
                <input class="r-input r-input--mono" type="text"
                       id="rns-msgs-newpeer"
                       placeholder="recipient hash (32 hex chars)"
                       maxlength="32" />
                <input class="r-input" type="text" id="rns-msgs-newtitle"
                       placeholder="(optional title)" />
                <textarea class="r-input rns-msgs__body" id="rns-msgs-newbody"
                          rows="3" placeholder="Type a message..."></textarea>
                <div class="rns-msgs__newactions">
                    <button class="r-btn r-btn--secondary"
                            id="rns-msgs-newcancel">Cancel</button>
                    <button class="r-btn r-btn--primary"
                            id="rns-msgs-newsend">Send</button>
                </div>
            </div>
        `;
        const peerInput = thread.querySelector('#rns-msgs-newpeer');
        peerInput.focus();
        thread.querySelector('#rns-msgs-newcancel').addEventListener(
            'click', () => this._selectedPeer
                ? this._renderThread(this._selectedPeer)
                : this._renderPlaceholder(),
        );
        thread.querySelector('#rns-msgs-newsend').addEventListener(
            'click', () => this._sendFromNewForm(),
        );
    }

    _renderPlaceholder() {
        const thread = this._root.querySelector('#rns-msgs-thread');
        thread.innerHTML = `
            <div class="rns-msgs__placeholder">
                Select a conversation, or click <b>+ New</b> to send a
                message to a peer hash.
            </div>
        `;
    }

    async _sendFromNewForm() {
        const peer = this._root.querySelector('#rns-msgs-newpeer').value.trim().toLowerCase();
        const title = this._root.querySelector('#rns-msgs-newtitle').value.trim();
        const body = this._root.querySelector('#rns-msgs-newbody').value.trim();
        if (!/^[0-9a-f]{32}$/.test(peer)) {
            this._api.toast('Recipient hash must be 32 lowercase hex characters');
            return;
        }
        if (!body) {
            this._api.toast('Message body cannot be empty');
            return;
        }
        await this._sendRaw(peer, title, body);
        this._selectedPeer = peer;
        // The WebSocket will re-fetch inbox; we also kick a fetch in case
        // the sent-log mtime watcher hasn't ticked yet.
        await this._fetchInbox();
    }

    async _send(peerHash) {
        const titleEl = this._root.querySelector(`#rns-msgs-title-${this._slug(peerHash)}`);
        const bodyEl = this._root.querySelector(`#rns-msgs-body-${this._slug(peerHash)}`);
        const title = (titleEl?.value || '').trim();
        const body = (bodyEl?.value || '').trim();
        if (!body) {
            this._api.toast('Message body cannot be empty');
            return;
        }
        await this._sendRaw(peerHash, title, body);
        if (bodyEl) bodyEl.value = '';
        if (titleEl) titleEl.value = '';
        await this._fetchInbox();
    }

    async _sendRaw(peerHash, title, body) {
        try {
            const res = await fetch('/api/reticulum/send', {
                method:  'POST',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({
                    destination_hash: peerHash,
                    title:            title || undefined,
                    content:          body,
                }),
            });
            if (!res.ok) {
                const errBody = await res.json().catch(() => ({}));
                throw new Error(errBody.detail || `HTTP ${res.status}`);
            }
            this._api.toast('Sent');
        } catch (e) {
            this._api.toast(`Send failed: ${e.message}`);
        }
    }

    // ── Live updates over the existing /ws WebSocket ────────────

    _connectWebSocket() {
        if (this._ws && this._ws.readyState !== WebSocket.CLOSED) return;
        try {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            this._ws = new WebSocket(`${proto}//${location.host}/ws`);
        } catch (e) {
            // Fallback to 30s polling already running -- no-op here.
            return;
        }
        this._ws.addEventListener('message', (ev) => {
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === 'lxmf_inbox_changed') {
                    // Don't trust the broadcast payload -- just re-fetch
                    // the canonical inbox. Cheaper for the server than
                    // sending the whole payload to every client.
                    this._fetchInbox();
                }
            } catch (e) {
                // Bad JSON, ignore.
            }
        });
        // Auto-reconnect on close. Cap retry frequency at 5s so a server
        // restart doesn't flood the network with reconnect attempts.
        this._ws.addEventListener('close', () => {
            setTimeout(() => this._connectWebSocket(), 5000);
        });
    }

    // ── Tiny helpers ────────────────────────────────────────────

    _setStatus(label) {
        const badge = this._root.querySelector('#rns-msgs-status');
        if (!badge) return;
        badge.textContent = label;
        if (label === 'LIVE') {
            badge.classList.remove('r-badge--muted');
        } else {
            badge.classList.add('r-badge--muted');
        }
    }

    _slug(hash) {
        // Reuse hash as DOM id suffix -- it's already a safe alnum string.
        return hash.slice(0, 12);
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
        el.textContent = str == null ? '' : String(str);
        return el.innerHTML;
    }
}

window.RnsMessagesCard = RnsMessagesCard;
