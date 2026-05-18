/**
 * Conversation list sidebar and contact picker for the messaging panel.
 * Loads conversations from the API, renders them in the sidebar,
 * and provides a modal for starting new conversations.
 */
class MessagingContacts {
    constructor(listEl, onSelect) {
        this._listEl = listEl;
        this._onSelect = onSelect;
        this._channels = [];
        this._conversations = [];
        this._activeNodeId = null;
        this._filter = 'all';
    }

    async load(includeOverheard = false) {
        try {
            const [convosRes, channelsRes, rnsRes] = await Promise.all([
                fetch(includeOverheard
                    ? '/api/messages/conversations?include_overheard=true'
                    : '/api/messages/conversations'),
                fetch('/api/messages/channels'),
                // Phase 1 #5: pull RNS messages in the same pass.
                // .catch returns null so a missing rnsd doesn't break
                // the MT/MC conversation list.
                fetch('/api/reticulum/inbox?limit=500').catch(() => null),
            ]);
            this._conversations = await convosRes.json();
            this._channels = await channelsRes.json();

            if (rnsRes && rnsRes.ok) {
                const rnsData = await rnsRes.json();
                this._mergeRnsConversations(rnsData.messages || []);
            }
            this.render();
        } catch (e) {
            console.error('Failed to load conversations:', e);
        }
    }

    _mergeRnsConversations(rnsMessages) {
        // RNS inbox is a flat list of messages with direction/peer_hash;
        // collapse into one conversation per peer (latest message wins
        // for preview + timestamp). Self-conversations (peer_hash ==
        // our own LXMF address) are kept -- they're useful for testing
        // and the user explicitly chose to keep them visible earlier
        // in the Channels card design.
        const byPeer = new Map();
        for (const m of rnsMessages) {
            const peer = m.peer_hash || '';
            if (!peer) continue;
            const existing = byPeer.get(peer);
            if (!existing || (m.timestamp || 0) > (existing.timestamp || 0)) {
                byPeer.set(peer, m);
            }
        }
        // Drop any stale RNS entries the previous load left behind,
        // then re-append from the fresh snapshot. Non-RNS conversations
        // are untouched.
        this._conversations = this._conversations.filter(
            c => c.protocol !== 'reticulum'
        );
        for (const [peer, latest] of byPeer) {
            this._conversations.push({
                node_id:        peer,
                node_name:      latest.peer_display_name
                                  || (peer.slice(0, 12) + '…'),
                protocol:       'reticulum',
                last_message:   (latest.direction === 'out' ? '↑ ' : '')
                                + (latest.content || ''),
                last_timestamp: latest.iso || '',
                unread_count:   0,
                is_broadcast:   false,
                peer_class:     latest.peer_class,
                // Phase 4 Y3: pass display_name_source so the
                // Messages-tab edit pencil can show whether the
                // current name came from the operator override
                // (lxmf_contacts.json) or from the classifier
                // (announce app_data).
                name_source:    latest.peer_display_name_source || 'none',
            });
        }
        this._sortByRecent();
    }

    render() {
        this._listEl.innerHTML = '';

        const filteredChannels = this._filter === 'all'
            ? this._channels
            : this._channels.filter(c => c.protocol === this._filter);

        if (filteredChannels.length > 0) {
            const label = document.createElement('div');
            label.className = 'msg-sidebar__section-label';
            label.textContent = 'Channels';
            this._listEl.appendChild(label);

            filteredChannels.forEach(ch => {
                const convo = this._channelToConvo(ch);
                const el = this._buildConvoEl(convo);
                this._listEl.appendChild(el);
            });
        }

        const dmConvos = (this._filter === 'all'
            ? this._conversations
            : this._conversations.filter(c => c.protocol === this._filter)
        ).filter(c => !c.is_broadcast);

        if (dmConvos.length > 0) {
            const label = document.createElement('div');
            label.className = 'msg-sidebar__section-label';
            label.textContent = 'Direct Messages';
            this._listEl.appendChild(label);

            dmConvos.forEach(convo => {
                const el = this._buildConvoEl(convo);
                this._listEl.appendChild(el);
            });
        }

        if (filteredChannels.length === 0 && dmConvos.length === 0) {
            this._listEl.innerHTML = '<div class="msg-chat__empty">No conversations yet</div>';
        }
    }

    _channelToConvo(ch) {
        const existing = this._conversations.find(c => c.node_id === ch.node_id);
        return {
            node_id: ch.node_id,
            node_name: ch.name,
            protocol: ch.protocol,
            channel: ch.channel || 0,
            is_broadcast: true,
            last_message: existing ? existing.last_message : '',
            last_timestamp: existing ? existing.last_timestamp : '',
            unread_count: existing ? existing.unread_count : 0,
        };
    }

    setFilter(protocol) {
        this._filter = protocol;
        this.render();
    }

    setActive(nodeId) {
        this._activeNodeId = nodeId;
        this._listEl.querySelectorAll('.msg-convo').forEach(el => {
            el.classList.toggle('msg-convo--active', el.dataset.nodeId === nodeId);
        });
    }

    addOrUpdateConversation(msg) {
        const existing = this._conversations.find(c => c.node_id === msg.node_id);
        if (existing) {
            existing.last_message = msg.text || msg.last_message || '';
            existing.last_timestamp = msg.timestamp || new Date().toISOString();
            if (msg.direction === 'received' && msg.node_id !== this._activeNodeId) {
                existing.unread_count = (existing.unread_count || 0) + 1;
            }
        } else {
            this._conversations.unshift({
                node_id: msg.node_id,
                node_name: msg.node_name || msg.node_id,
                protocol: msg.protocol || 'meshtastic',
                last_message: msg.text || '',
                last_timestamp: msg.timestamp || new Date().toISOString(),
                unread_count: msg.direction === 'received' ? 1 : 0,
                is_broadcast: (msg.node_id || '').startsWith('broadcast:'),
            });
        }
        this._sortByRecent();
        this.render();
        if (this._activeNodeId) this.setActive(this._activeNodeId);
    }

    async openContactPicker() {
        try {
            const res = await fetch('/api/messages/contacts');
            const contacts = await res.json();
            this._showModal(contacts);
        } catch (e) {
            console.error('Failed to load contacts:', e);
        }
    }

    _buildConvoEl(convo) {
        const el = document.createElement('div');
        el.className = 'msg-convo';
        if (convo.is_broadcast) el.classList.add('msg-convo--channel');
        if (convo.node_id === this._activeNodeId) el.classList.add('msg-convo--active');
        el.dataset.nodeId = convo.node_id;

        const isChannel = !!convo.is_broadcast;
        // Phase 1 #5: third icon variant for Reticulum. Cyan to match
        // the RNS palette used elsewhere (Channels card, Nodes panel).
        let iconClass;
        if (isChannel) iconClass = 'msg-convo__icon--channel';
        else if (convo.protocol === 'meshcore')  iconClass = 'msg-convo__icon--mc';
        else if (convo.protocol === 'reticulum') iconClass = 'msg-convo__icon--rns';
        else iconClass = 'msg-convo__icon--mt';

        const iconText = isChannel
            ? '#'
            : (convo.node_name || '?').slice(0, 2).toUpperCase();

        const displayName = isChannel
            ? convo.node_name || `Ch ${convo.channel || 0}`
            : convo.node_name || convo.node_id;

        let protoBadge, badgeKey;
        if (convo.protocol === 'meshcore')        { protoBadge = 'MC';  badgeKey = 'mc';  }
        else if (convo.protocol === 'reticulum')  { protoBadge = 'RNS'; badgeKey = 'rns'; }
        else                                       { protoBadge = 'MT';  badgeKey = 'mt';  }

        // Phase 4 Y3: edit-name pencil for RNS rows only. MT/MC
        // names come from a different source (Meshtastic NodeInfo,
        // MC companion contacts) so the lxmf_contacts.json override
        // path doesn't apply. Pencil is opaque when an operator
        // override is active, faint otherwise (hover to discover).
        let editPencil = '';
        if (convo.protocol === 'reticulum' && !isChannel) {
            const opaque = (convo.name_source === 'operator');
            const cls = opaque
                ? 'msg-convo__edit-pencil msg-convo__edit-pencil--set'
                : 'msg-convo__edit-pencil msg-convo__edit-pencil--hint';
            const title = opaque
                ? 'Operator-set nickname; click to edit'
                : 'Click to set a nickname for this peer';
            editPencil = `<span class="${cls}" title="${title}">✎</span>`;
        }

        const timeStr = convo.last_timestamp
            ? new Date(convo.last_timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : '';

        el.innerHTML = `
            <div class="msg-convo__icon ${iconClass}">${iconText}</div>
            <div class="msg-convo__info">
                <div class="msg-convo__name">${this._esc(displayName)} <span class="msg-convo__proto-badge msg-convo__proto-badge--${badgeKey}">${protoBadge}</span>${editPencil}</div>
                <div class="msg-convo__preview">${this._esc(convo.last_message || '')}</div>
            </div>
            <div class="msg-convo__meta">
                <div class="msg-convo__time">${timeStr}</div>
                ${convo.unread_count > 0 ? `<div class="msg-convo__unread">${convo.unread_count}</div>` : ''}
                <button class="msg-convo__delete" title="Delete conversation">&times;</button>
            </div>
        `;

        el.querySelector('.msg-convo__delete').addEventListener('click', (e) => {
            e.stopPropagation();
            this._deleteConversation(convo);
        });

        // Phase 4 Y3: pencil click → inline edit; everything else
        // on the row → open conversation. Pencil handler stops
        // propagation so the conversation-open doesn't also fire.
        const pencilEl = el.querySelector('.msg-convo__edit-pencil');
        if (pencilEl) {
            pencilEl.addEventListener('click', (ev) => {
                ev.stopPropagation();
                this._openInlineEdit(el, convo);
            });
        }

        el.addEventListener('click', () => {
            this.setActive(convo.node_id);
            this._onSelect(convo);
        });
        return el;
    }

    // ── Phase 4 Y3: inline edit of operator-set RNS nicknames ──────────
    _openInlineEdit(el, convo) {
        // Replace the .msg-convo__info content with an input + buttons.
        // Guard against opening twice if the operator clicks the pencil
        // again before they finish.
        const info = el.querySelector('.msg-convo__info');
        if (!info || info.querySelector('.msg-convo__edit-input')) return;

        const hasOverride = (convo.name_source === 'operator');
        info.innerHTML = `
            <div class="msg-convo__edit-form">
                <input type="text" class="msg-convo__edit-input"
                       value="${this._esc(convo.node_name || '')}"
                       placeholder="Nickname"
                       maxlength="64" />
                <button class="msg-convo__edit-save" title="Save (Enter)">Save</button>
                <button class="msg-convo__edit-cancel" title="Cancel (Esc)">&times;</button>
                ${hasOverride
                    ? '<button class="msg-convo__edit-revert" title="Revert to announce name">&#x21BA;</button>'
                    : ''
                }
            </div>
        `;
        const input = info.querySelector('.msg-convo__edit-input');
        input.focus();
        input.select();

        // Stop conversation-open clicks from firing while edit is open.
        ['click', 'mousedown'].forEach(evt =>
            info.querySelector('.msg-convo__edit-form')
                .addEventListener(evt, (e) => e.stopPropagation())
        );

        const closeAndReload = () => this.load();

        info.querySelector('.msg-convo__edit-cancel')
            .addEventListener('click', closeAndReload);
        info.querySelector('.msg-convo__edit-save')
            .addEventListener('click', () =>
                this._saveContact(convo.node_id, input.value, closeAndReload));
        const revertBtn = info.querySelector('.msg-convo__edit-revert');
        if (revertBtn) {
            revertBtn.addEventListener('click', () =>
                this._deleteContact(convo.node_id, closeAndReload));
        }
        input.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter')  this._saveContact(convo.node_id, input.value, closeAndReload);
            if (ev.key === 'Escape') closeAndReload();
        });
    }

    async _saveContact(hash, nickname, done) {
        const nick = (nickname || '').trim();
        if (!nick) { done(); return; }
        try {
            const res = await fetch(`/api/reticulum/contacts/${hash}`, {
                method:  'PUT',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({nickname: nick}),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                console.error('Save nickname failed:', err.detail || res.status);
            }
        } catch (e) {
            console.error('Save nickname network error:', e);
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
                console.error('Revert nickname failed:', err.detail || res.status);
            }
        } catch (e) {
            console.error('Revert nickname network error:', e);
        }
        done();
    }

    _showModal(contacts) {
        const overlay = document.createElement('div');
        overlay.className = 'msg-modal-overlay';

        const modal = document.createElement('div');
        modal.className = 'msg-modal';
        modal.innerHTML = `
            <div class="msg-modal__header">
                <span class="msg-modal__title">New Conversation</span>
                <button class="msg-modal__close">&times;</button>
            </div>
            <input class="msg-modal__search" placeholder="Search nodes..." />
            <div class="msg-modal__list"></div>
        `;

        const list = modal.querySelector('.msg-modal__list');
        const search = modal.querySelector('.msg-modal__search');

        const renderContacts = (filter) => {
            list.innerHTML = '';
            const filtered = filter
                ? contacts.filter(c => (c.name || '').toLowerCase().includes(filter) || (c.node_id || '').toLowerCase().includes(filter))
                : contacts;

            filtered.forEach(contact => {
                const item = document.createElement('div');
                item.className = 'msg-contact';
                const pClass = contact.protocol === 'meshcore' ? 'msg-contact__protocol--mc' : 'msg-contact__protocol--mt';
                item.innerHTML = `
                    <span class="msg-contact__name">${this._esc(contact.name || contact.node_id)}</span>
                    <span class="msg-contact__protocol ${pClass}">${contact.protocol === 'meshcore' ? 'MC' : 'MT'}</span>
                `;
                item.addEventListener('click', () => {
                    overlay.remove();
                    this._onSelect({
                        node_id: contact.node_id,
                        node_name: contact.name || contact.node_id,
                        protocol: contact.protocol,
                        is_broadcast: false,
                    });
                });
                list.appendChild(item);
            });
        };

        renderContacts('');
        search.addEventListener('input', () => renderContacts(search.value.toLowerCase()));
        modal.querySelector('.msg-modal__close').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        search.focus();
    }

    async _deleteConversation(convo) {
        try {
            const res = await fetch(`/api/messages/conversation/${encodeURIComponent(convo.node_id)}`, {
                method: 'DELETE',
            });
            if (!res.ok) return;
            this._conversations = this._conversations.filter(c => c.node_id !== convo.node_id);
            if (this._activeNodeId === convo.node_id) {
                this._activeNodeId = null;
                this._onSelect(null);
            }
            this.render();
        } catch (e) {
            console.error('Failed to delete conversation:', e);
        }
    }

    _sortByRecent() {
        this._conversations.sort((a, b) => {
            const ta = a.last_timestamp ? new Date(a.last_timestamp).getTime() : 0;
            const tb = b.last_timestamp ? new Date(b.last_timestamp).getTime() : 0;
            return tb - ta;
        });
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str || '';
        return el.innerHTML;
    }
}
