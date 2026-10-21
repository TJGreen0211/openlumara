// the last few chats as the server sent them, most recently shown last, so
// switching back to one shows it instantly while the fresh copy loads.
// kept outside the store so Alpine doesn't make it reactive
const CHAT_CACHE = new Map();
const CHAT_CACHE_SIZE = 8;

// a long chat renders only its newest turns at first; older ones render in
// batches when scrolled up to (each turn's markdown + highlighting is costly)
const INITIAL_RENDERED_TURNS = 30;
const OLDER_TURNS_BATCH = 20;

CHAT_STORE = {
    /*
     * alpine.js store for chat state
     */

    visibleChats: [],
    chatOffset: 0,
    chatLimit: 10,
    hasMoreChats: true,

    categories: [],
    chat: {},
    selectedChat: null,
    selectedCategory: 'general',

    draggedChatId: null,
    draggedChatCategory: null,
    dragHoverCategory: null,
    categoryCounts: {},

    // "moved to X · undo" toast shown after a chat is dropped on a category
    moveToast: null,
    _moveToastTimer: null,

    turnHistory: [],
    // turns before this index stay unrendered until scrolled up to
    renderStart: 0,
    _historyPaging: false,
    // a switch to a chat that isn't cached yet: shows the loading skeleton
    chatLoading: false,
    _switching: false,
    _loadSeq: 0,
    _chatsSeq: 0,
    editingMessageIndex: null,
    editContent: '',
    editAttached: [],

    user_input: '',
    last_user_input: '',
    _sendAckTimer: null,
    _reloadSeq: 0,

    currentTokenUsage: 0,

    async load() {
        // called by Alpine.init
        // always land in the store-default category ('general'),
        // even if the backend's current chat belongs to a different category

        // the sidebar lists load alongside the chat; the chat shows as soon as it arrives
        const lists = Promise.allSettled([this.reloadChats(), this.reloadCategories()]);

        const result = await simpleApiFetch(`/api/chat/current`);
        if (!result) { return }

        this._showChat(result);
        await lists;

        // ensure the chat exists in the visible sidebar list before scrolling
        // (only possible when it belongs to the currently selected category)
        if (result.category === this.selectedCategory) {
            await this.ensureChatVisible(this.selectedChat);
        }
    },

    /* ----------------------
     * chat manipulation
     * ----------------------- */
    async loadChat(chatId) {
        if (this.selectedChat === chatId) { return; }

        // don't allow chat switching if a stream is ongoing
        if (Alpine.store("stream").state != 'idle') { return; }

        // switch the view on the click and let the network catch up: a chat
        // shown recently comes straight from memory, any other gets a loading
        // skeleton. selecting it first also turns the chat_switched echo of
        // this same load (which can beat the http response) into a no-op
        // instead of a second full load
        const seq = ++this._loadSeq;
        // a reload still in flight for the previous chat must not land on this one
        this._reloadSeq++;
        this._switching = true;

        const previous = {
            chat: this.chat,
            selectedChat: this.selectedChat,
            selectedCategory: this.selectedCategory,
            turnHistory: this.turnHistory,
            renderStart: this.renderStart,
            currentTokenUsage: this.currentTokenUsage,
        };

        const cached = CHAT_CACHE.get(chatId);
        if (cached) {
            this.selectedCategory = cached.category;
            this._showChat(cached);
        } else {
            // the sidebar entry has the title and category, so the header is right immediately
            const listed = this.visibleChats.find(c => c.id === chatId);
            this.chat = listed ? { ...listed } : { id: chatId };
            this.selectedChat = chatId;
            if (listed) {
                this.selectedCategory = listed.category;
                this.currentTokenUsage = listed.token_usage;
            }
            this.turnHistory = [];
            this.renderStart = 0;
            this.chatLoading = true;
        }

        let result = null;
        try {
            // the backend tags its chat_switched broadcast with this, so this
            // tab can ignore the echo of its own switch
            result = await simpleApiFetch(`/api/chat/load/${chatId}`, { headers: { 'X-Client-Id': CLIENT_ID } });
        } catch (e) {
            console.error('failed to load chat:', e);
        }

        // the user already clicked another chat
        if (seq !== this._loadSeq) { return; }
        this.chatLoading = false;
        this._switching = false;

        if (!result) {
            // the backend is still on the previous chat, so the view goes back to it
            for (const [key, value] of Object.entries(previous)) {
                this[key] = value;
            }
            return;
        }

        this.selectedCategory = result.category;
        // for a cached chat this only patches the turns that changed
        this._showChat(result);
    },

    async followSwitch(chatId) {
        /*
         * the backend already switched chats (another tab, a meeting
         * transcript...): show its current chat without asking it to switch.
         * going through loadChat here re-requested the switch, which
         * broadcast it again, so a few rapid clicks bounced every tab
         * between those chats forever
         */
        if (this.selectedChat === chatId) { return; }
        if (Alpine.store("stream").state != 'idle') { return; }
        // this tab's own switch is in flight and will land after this event
        if (this._switching) { return; }

        const seq = ++this._loadSeq;
        this._reloadSeq++;
        const result = await simpleApiFetch(`/api/chat/current`);
        if (!result || seq !== this._loadSeq) { return; }

        this.selectedCategory = result.category;
        this._showChat(result);
    },

    _supersedeLoads() {
        // the backend just switched chats on its own (new chat): a switch or
        // reload still in flight is stale and must not land
        this._loadSeq++;
        this._reloadSeq++;
        this._switching = false;
        this.chatLoading = false;
    },

    _showChat(result) {
        /*
         * puts a chat payload from the server on screen. reloading the chat
         * that's already showing keeps every turn that didn't change as the
         * same object, so Alpine leaves its DOM (and rendered markdown) alone
         * and only new or edited turns render
         */
        const sameChat = this.chat?.id === result.id && this.turnHistory.length > 0;
        const turns = [...(this._withoutActiveStream(result.turn_history) || [])];

        this.chat = result;
        this.selectedChat = result.id;
        this.currentTokenUsage = result.token_usage;

        if (sameChat) {
            this.turnHistory = this._mergeTurns(turns);
            // a deleted turn can shrink the history below the rendered window
            this.renderStart = Math.min(this.renderStart, Math.max(0, turns.length - INITIAL_RENDERED_TURNS));
        } else {
            this.turnHistory = turns;
            this.renderStart = Math.max(0, turns.length - INITIAL_RENDERED_TURNS);
            this._startHistoryPaging();
        }

        Alpine.store('artifact').restoreFromHistory(this.selectedChat, this.turnHistory);

        CHAT_CACHE.delete(result.id);
        CHAT_CACHE.set(result.id, result);
        if (CHAT_CACHE.size > CHAT_CACHE_SIZE) {
            CHAT_CACHE.delete(CHAT_CACHE.keys().next().value);
        }
    },

    _mergeTurns(incoming) {
        // turns are identified by their first message index; a delete or edit
        // shifts or changes them, which the content comparison catches
        const current = new Map();
        for (const turn of Alpine.raw(this.turnHistory)) {
            if (turn.first_message_index != null) {
                current.set(turn.first_message_index, turn);
            }
        }

        return incoming.map((turn) => {
            const existing = current.get(turn.first_message_index);
            return existing && JSON.stringify(existing) === JSON.stringify(turn) ? existing : turn;
        });
    },

    async _startHistoryPaging() {
        // paging stays off until the new chat is scrolled to the bottom: right
        // after it renders, the loader at the top is briefly in view
        this._historyPaging = false;
        const seq = this._loadSeq;

        const ui = Alpine.store('ui');
        await ui.forceScrollToBottom();
        await Alpine.nextTick();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        if (seq !== this._loadSeq) { return; }

        this._historyPaging = true;

        // a chat whose newest turns don't fill the screen never scrolls, so
        // the loader would never come into view: fill the screen up front
        // (a hidden container measures 0 tall and would render everything)
        const el = document.getElementById('messages');
        while (el && el.clientHeight > 0 && this.renderStart > 0 && el.scrollHeight <= el.clientHeight) {
            await this.renderOlderTurns();
        }
    },

    async renderOlderTurns() {
        // called when the loader above the oldest rendered turn scrolls into view
        if (!this._historyPaging || this.renderStart <= 0) { return; }

        const el = document.getElementById('messages');
        if (!el) { return; }

        // keep the turns the user is looking at in place while older ones appear above them
        const fromBottom = el.scrollHeight - el.scrollTop;
        this.renderStart = Math.max(0, this.renderStart - OLDER_TURNS_BATCH);
        await Alpine.nextTick();
        // the user may have jumped to the bottom while these were rendering;
        // restoring the old position would throw them back up
        el.scrollTop = Alpine.store('ui').shouldScroll ? el.scrollHeight : el.scrollHeight - fromBottom;
    },

    async reloadChats() {
        // fetch before swapping, so the list never blanks out (which also made
        // every sidebar item rebuild), and keep as many chats as were showing
        // so a sidebar scrolled down doesn't jump back to the top
        const seq = ++this._chatsSeq;
        const result = await this._fetchChatsPage(0, Math.max(this.chatLimit, this.chatOffset));
        if (!result || seq !== this._chatsSeq) { return; }

        this.visibleChats = result.messages;
        this.chatOffset = result.messages.length;
        this.hasMoreChats = result.has_more;

        // ensure there are always more chats loaded than what fits in the current viewport
        await this.ensureMoreChats();
    },

    async ensureChatVisible(chatId) {
        const exists = this.visibleChats.some(c => c.id === chatId);
        if (exists) { return; }
        
        // keep loading more chats until the target chat appears
        while (this.hasMoreChats && !this.visibleChats.some(c => c.id === chatId)) {
            await this.loadMoreChats();
        }
    },

    async loadMoreChats() {
        if (!this.hasMoreChats) { return; }
        await this._fetchChats();
    },

    async ensureMoreChats(el) {
        /* 
         * makes sure there are always more chats loaded
         * than what the viewport can show,
         * so that x-intersect always works (because it needs to be out of view first)
         */
        const intersect_el = document.getElementById("chat-scroll-loader");
        if (!intersect_el) { return; }

        const rect = intersect_el.getBoundingClientRect();

        if (rect.top < window.innerHeight && rect.bottom > 0) {
            await this.loadMoreChats();
        }
    },

    async _fetchChats() {
        const seq = this._chatsSeq;
        const result = await this._fetchChatsPage(this.chatOffset, this.chatLimit);
        // a reload replaced the list meanwhile; this page would duplicate or misplace chats
        if (!result || seq !== this._chatsSeq) { return; }

        this.visibleChats.push(...result.messages);
        this.chatOffset += result.messages.length;
        this.hasMoreChats = result.has_more;
    },

    async _fetchChatsPage(offset, limit) {
        const catParam = this.selectedCategory ? `&category=${encodeURIComponent(this.selectedCategory)}` : '';
        return await simpleApiFetch(`/api/chats?offset=${offset}&limit=${limit}${catParam}`);
    },

    async newChat() {
        // the open chat is already a blank one in this category: reuse it
        // instead of piling up empty "New chat" entries
        if (
            this.selectedChat &&
            this.turnHistory.length === 0 &&
            this.chat.category === this.selectedCategory &&
            !this.chat.metadata?.meeting_room
        ) {
            document.getElementById('user-input')?.focus();
            return;
        }

        await simpleApiPost('/api/chat/new', { category: this.selectedCategory });

        this._supersedeLoads();
        const result = await simpleApiFetch('/api/chat/current');
        if (!result) { return; }

        this.selectedCategory = result.category;
        this._showChat(result);

        // the chat itself was just fetched, so only the sidebar needs a refresh
        await Promise.all([this.reloadChats(), this.reloadCategories()]);
    },

    async newCategory(categoryName, metadata = null) {
        await simpleApiPost('/api/chat/new', { category: categoryName, metadata });

        this._supersedeLoads();
        const result = await simpleApiFetch('/api/chat/current');
        if (!result) { return; }

        this.selectedCategory = result.category;
        this._showChat(result);

        await Promise.all([this.reloadChats(), this.reloadCategories()]);
    },

    async renameChat(chat_id, newTitle) {
        await simpleApiPost(`/api/chat/rename/${chat_id}`, {title: newTitle});
        if (chat_id === this.selectedChat) {
            // keep the titlebar in sync with the sidebar
            this.chat.title = newTitle;
        }
        await this.reloadChats();
    },

    async deleteChat(chat_id) {
        if (!confirm("Are you sure you want to delete this chat?")) { return }

        await simpleApiPost(`/api/chat/delete/${chat_id}`);
        CHAT_CACHE.delete(chat_id);

        // deleting the open chat makes the backend switch to another one;
        // follow it, or the view keeps showing the deleted chat while new
        // messages go to the backend's new current chat
        if (chat_id === this.selectedChat) {
            await this.reloadChat();
            this.currentTokenUsage = this.chat.token_usage;
        }

        await this.reloadChats();
        await this.reloadCategories();
    },

    async moveChatToCategory(chatId, targetCategory) {
        if (chatId === null || targetCategory === null) return;
        if (chatId && targetCategory && this.draggedChatCategory === targetCategory) return;

        await simpleApiPost(`/api/chat/category/${chatId}`, { category: targetCategory });

        if (this.selectedChat === chatId) {
            this.selectedCategory = targetCategory;
            this.chat.category = targetCategory;
        }

        await this.reloadChats();
        await this.reloadCategories();
    },

    async dropChatOnCategory(category) {
        const chatId = this.draggedChatId;
        const fromCategory = this.draggedChatCategory;
        this.draggedChatId = null;
        this.draggedChatCategory = null;
        this.dragHoverCategory = null;

        if (!chatId || category === fromCategory) return;
        if (Alpine.store('stream').state !== 'idle') return;

        await this.moveChatToCategory(chatId, category);
        this.showMoveToast(chatId, fromCategory, category);
    },

    showMoveToast(chatId, fromCategory, toCategory) {
        clearTimeout(this._moveToastTimer);
        this.moveToast = { chatId, fromCategory, toCategory };
        this._moveToastTimer = setTimeout(() => { this.moveToast = null; }, 5000);
    },

    async undoMove() {
        const toast = this.moveToast;
        if (!toast) return;
        clearTimeout(this._moveToastTimer);
        this.moveToast = null;
        await this.moveChatToCategory(toast.chatId, toast.fromCategory);
    },

    categoryLabel(category) {
        return category === 'general' ? 'General' : category;
    },

    async reloadChat() {
        if (!this.selectedChat) {
            console.log("tried to reload the chat, but no chat is loaded!");
            return;
        }

        // overlapping reloads (a reconnect, a sync and a stream_complete can all
        // fire one) may resolve out of order on a slow network; only the most
        // recently started one may write, or an older snapshot clobbers a newer one
        const seq = ++this._reloadSeq;
        const result = await simpleApiFetch(`/api/chat/current`);
        // while a chat switch is in flight the backend may still be on the old
        // chat; the switch fetches the new one fresh anyway
        if (!result || seq !== this._reloadSeq || this._switching) { return }

        // note: deliberately not touching selectedCategory here - a plain
        // data refresh must not change the category the user is looking at
        this._showChat(result);
    },

    _withoutActiveStream(turns) {
        // while a resumed stream is running, the backend history already holds
        // its intermediate (tool call) messages, which the live stream bubble shows
        const cutoff = Alpine.store('stream').cutoffIndex;
        if (cutoff == null || !Array.isArray(turns)) { return turns; }
        return turns.filter(t => t.first_message_index == null || t.first_message_index <= cutoff);
    },

    async reloadCategories() {
        const [categories, counts] = await Promise.all([
            simpleApiFetch('/api/chats/categories'),
            simpleApiFetch('/api/chats/category_counts'),
        ]);
        this.categories = categories;
        this.categoryCounts = counts || {};
    },

    async selectCategory(category) {
        this.selectedCategory = category;
        await this.reloadChats();
    },

    async clearInput() {
        // store the last user input for use in things like placeholder message bubbles
        this.last_user_input = this.user_input;
        this.user_input = '';
    },


    async send(text) {
        const stream = Alpine.store('stream');
        if (stream.state !== 'idle') {
            // don't allow sending during a stream
            // (TODO: allow nudging (interrupting the stream and sending a new message))
            return;
        }

        Alpine.store("stream").state = "message_sending";

        // still inside the send click/keypress, so the browser allows the prompt.
        // not awaited: the message shouldn't wait on the user answering it
        Alpine.store('notifications').maybeRequestPermission();

        await this.clearInput();

        // handle any files the user may have attached
        const uploadStore = Alpine.store("upload");

        let files = null;

        if (uploadStore.files.length > 0) {
            files = await Promise.all(
                uploadStore.files.map(async (file) => ({
                    name: file.name,
                    data: await uploadStore.readFileAsBase64(file)
                }))
            );
        }

        AudioManager.play("send_message");

        /*
         * send the message to the backend - websockets will take it from here
         * the backend will now emit user_message_added to confirm the user message was received by the backend,
         * which the frontend (services/websockets.js) receives and then triggers reloadChat() on this chat store
         * so that the new user message shows up
         */
        const success = await simpleSocketSend({
            type: "user_message",
            content: text,
            files: files,
            // image/video attachments also get saved into the site open in the
            // preview panel, so the AI can use them in the html
            site: files ? Alpine.store('artifact').attachTarget : null
        });

        if (!success) {
            // the websocket is gone; the payload would have been dropped
            // silently. put the message back so it isn't lost.
            this.user_input = text;
            stream.state = 'idle';
            return;
        }

        // the backend confirms receipt with user_message_added. if that never
        // arrives the socket was half-dead (TCP gone but readyState still
        // OPEN), so restore the message and force a reconnect instead of
        // silently eating it.
        this._sendAckTimer = setTimeout(() => this._checkSendAck(text), 10000);

        uploadStore.clear();
    },

    _checkSendAck(text) {
        this._sendAckTimer = null;

        const stream = Alpine.store('stream');
        if (stream.state !== 'message_sending') {
            // the backend acknowledged, or the stream already resolved
            return;
        }

        // no ack within the window: the send went into a dead socket.
        // restore the message and close the socket so onclose reconnects.
        if (this.user_input) {
            // the user already started a new draft in the meantime; keep both
            this.user_input = text + '\n\n' + this.user_input;
        } else {
            this.user_input = text;
        }
        stream.state = 'idle';
        // not socket.close(): on a dead socket that waits on a close handshake
        // that never comes, and nothing reconnects until it gives up
        forceWsReconnect();
    },

    _clearSendAck() {
        if (this._sendAckTimer) {
            clearTimeout(this._sendAckTimer);
            this._sendAckTimer = null;
        }
    },

    async transcribeVoice() {
        const voice = Alpine.store('voice');

        if (voice.recording) {
            // stops capture and commits the accumulated transcription into the input
            await voice.stopRecording();
        } else {
            // starts capture; live previews show in the caption strip, the input
            // field only receives the text once the session ends
            await voice.startRecording();
        }
    },

    /* ----------------------
     * message actions
     * ----------------------- */
    async copyMessage(turnIndex) {
      const turn = this.turnHistory[turnIndex];
      const msg = turn?.messages?.[turn.messages?.length - 1]; // last message in the turn
      if (!msg) return false;
      // return the promise so the caller's success checkmark only shows when
      // the clipboard write actually succeeded
      return navigator.clipboard.writeText(msg.content)
        .then(() => {
            return true;
        })
        .catch(err => {
            console.error('clipboard write failed:', err);
            return false;
        });
    },

    async deleteMessage(turnIndex) {
        const turn = this.turnHistory[turnIndex];
        if (!turn) return;
        await simpleSocketSend({
            "type": "message_delete",
            "index": turn.first_message_index
        });
    },

    async regenerateMessage(turnIndex) {
        const turn = this.turnHistory[turnIndex];
        if (!turn) return;

        Alpine.store('stream').userMsg = null;
        Alpine.nextTick(async () => {
            await simpleSocketSend({
                "type": "message_regenerate",
                "index": turn.first_message_index
            });

            Alpine.store('stream').state = 'message_sending';
        });
    },

    async startEdit(turnIndex) {
        const turn = this.turnHistory[turnIndex];
        const msg = turn?.messages?.[turn.messages?.length - 1]; // last message in the turn
        if (!msg) { return; }
        
        this.editingMessageIndex = msg.index;
        // multimodal messages store content as blocks; edit only the user's text block
        this.editContent = Array.isArray(msg.content)
            ? (msg.content.find((b, i) => b.type === 'text' && !msg._metadata?.filenames?.[i])?.text ?? '')
            : msg.content;
        // existing attachments (names only; new files carry raw File objects)
        this.editAttached = (msg._metadata?.filenames || []).filter(f => f).map(name => ({ name }));
        Alpine.store('ui').scrollToTurnIndex = turnIndex;
    },

    async cancelEdit() {
        this.editingMessageIndex = null;
        this.editContent = '';
        this.editAttached = [];
    },

    addEditFile(event) {
        for (const file of event.target.files) {
            if (!this.editAttached.some(e => e.name === file.name)) {
                this.editAttached.push({ name: file.name, file });
            }
        }
        event.target.value = "";
    },

    removeEditFile(name) {
        this.editAttached = this.editAttached.filter(e => e.name !== name);
    },

    async saveEdit(index) {
        // rebuild multimodal content: replace the user's text block, keep file blocks
        const msg = this.turnHistory.flatMap(t => t.messages || []).find(m => m.index === index);
        let content = this.editContent;
        let filenames = null;

        if (msg && Array.isArray(msg.content)) {
            const blocks = [];
            const names = [];
            let replacedText = false;
            msg.content.forEach((block, i) => {
                const fname = msg._metadata?.filenames?.[i];
                if (fname && !this.editAttached.some(e => e.name === fname)) { return; } // file removed
                if (block.type === 'text' && !fname && !replacedText) {
                    replacedText = true;
                    blocks.push({ ...block, text: this.editContent });
                } else {
                    blocks.push(block);
                }
                names.push(fname || '');
            });
            if (!replacedText && this.editContent) {
                blocks.unshift({ type: 'text', text: this.editContent });
                names.unshift('');
            }
            content = blocks;
            filenames = names;
        }

        // newly attached files (raw File objects) - converted to blocks by the backend
        const newFiles = this.editAttached.filter(e => e.file);
        let files = null;
        if (newFiles.length > 0) {
            const uploadStore = Alpine.store("upload");
            files = await Promise.all(newFiles.map(async (e) => ({
                name: e.name,
                data: await uploadStore.readFileAsBase64(e.file)
            })));
        }

        await simpleSocketSend({
            "type": "message_edit",
            "index": index,
            "content": content,
            "filenames": filenames,
            "files": files
        });

        this.editingMessageIndex = null;
        this.editContent = '';
        this.editAttached = [];
    },

    /* ----------------------
     * chat export
     * ----------------------- */
    async export() {
        try {
            // Get the export string from the backend
            const exportStr = await simpleApiFetch('/api/chat/export');
            
            if (!exportStr) {
                throw new Error('Export returned empty data');
            }

            // Get chat title for filename
            const chatTitle = this.chat?.title || 'chat-export';
            const safeTitle = chatTitle.replace(/[\/\\:*?"<>|]/g, '_');
            const filename = `${safeTitle}.txt`;

            // Create blob and trigger download
            const blob = new Blob([exportStr], { type: 'text/plain' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (err) {
            console.error('Export failed:', err);
            // Optional: show a toast/notification to the user
        }
    },

    /* ----------------------
     * global search
     * ----------------------- */
    async searchGlobal(query, searchInContent = true, category = null) {
        try {
            const result = await simpleApiPost('/api/chats/search', {
                query: query,
                search_in_content: searchInContent,
                category: category
            });
            
            const queryLower = (query || '').toLowerCase();

            // Priority sort:
            // 1. Chats whose title contains the query come first
            // 2. Within each group, sorted by updated descending (newest first)
            result.sort((a, b) => {
                const aMatches = (a.title || '').toLowerCase().includes(queryLower);
                const bMatches = (b.title || '').toLowerCase().includes(queryLower);

                // Primary: matching titles first
                if (aMatches && !bMatches) return -1;
                if (!aMatches && bMatches) return 1;

                // Secondary: both match or both don't → sort by date descending
                return (b.updated || '').localeCompare(a.updated || '');
            });

            return result;
        } catch (err) {
            console.error('Global search failed:', err);
            return [];
        }
    },

    async loadChatFromSearch(chatId) {
        await this.loadChat(chatId);
        Alpine.store('ui').closeModal();
        if (Alpine.store('ui').isMobile) {
            Alpine.store('ui').showSidebar = false;
        }
    },

    /* ----------------------
     * chat-specific getters
     * ----------------------- */
    get promptprogress() {
        // does the math for the prompt processing indicator over in components/promptprocess.html
        // the math was ported straight over from the old webUI because, well, it works, and it's clean code
        const progressData = Alpine.store("stream").processing;

        const cache = progressData.cache || 0;
        const processed = progressData.processed - cache;
        const total = progressData.total - cache;
        const percent = total > 0 ? Math.round((processed / total) * 100) : 0;
        const elapsed = progressData.time_ms / 1000;
        const remaining = (total - processed) > 0 ? (elapsed / processed) * (total - processed) : 0;

        return {
            cache,
            processed,
            total,
            percent,
            percent_str: `${percent}%`,
            elapsed: elapsed.toFixed(1),
            remaining,
            remaining_str: `(ETA: ${Math.ceil(remaining)}s)`
        };
    }
}
