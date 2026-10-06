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

    turnHistory: [],
    editingMessageIndex: null,
    editContent: '',
    editAttached: [],

    user_input: '',
    last_user_input: '',
    _sendAckTimer: null,

    currentTokenUsage: 0,

    async load() {
        // called by Alpine.init
        // always land in the store-default category ('general'),
        // even if the backend's current chat belongs to a different category
        await this.reloadChats();
        await this.reloadCategories();

        const result = await simpleApiFetch(`/api/chat/current`);
        if (!result) { return }

        this.chat = result;
        this.selectedChat = result.id;
        this.turnHistory = result.turn_history;
        this.currentTokenUsage = result.token_usage;
        Alpine.store('artifact').restoreFromHistory(this.selectedChat, this.turnHistory);

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

        const result = await simpleApiFetch(`/api/chat/load/${chatId}`);
        if (!result) { return; }

        this.chat = result;
        this.selectedChat = chatId;
        this.selectedCategory = result.category;
        this.turnHistory = result.turn_history;
        Alpine.store('artifact').restoreFromHistory(this.selectedChat, this.turnHistory);

        ui = Alpine.store('ui');
        this.currentTokenUsage = result.token_usage;

        // make sure it always shows the bottom of the chat
        await ui.forceScrollToBottom();
    },

    async reloadChats() {
        this.chatOffset = 0;
        this.visibleChats = [];
        await this._fetchChats();

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
        const offset = this.chatOffset;
        const catParam = this.selectedCategory ? `&category=${encodeURIComponent(this.selectedCategory)}` : '';
        const result = await simpleApiFetch(`/api/chats?offset=${offset}&limit=${this.chatLimit}${catParam}`);
        if (!result) { return; }

        this.visibleChats.push(...result.messages);
        this.chatOffset += result.messages.length;
        this.hasMoreChats = result.has_more;
    },

    async newChat() {
        await simpleApiPost('/api/chat/new', { category: this.selectedCategory });

        result = await simpleApiFetch('/api/chat/current');
        if (!result) { return; }

        this.chat = result;

        this.selectedChat = result.id;
        this.selectedCategory = result.category;
        this.currentTokenUsage = result.token_usage;
        this.turnHistory = result.turn_history;

        await this.reloadChats();
        await this.reloadChat();
    },

    async newCategory(categoryName) {
        await simpleApiPost('/api/chat/new', { category: categoryName });

        const result = await simpleApiFetch('/api/chat/current');
        if (!result) { return; }

        this.chat = result;
        this.selectedChat = result.id;
        this.selectedCategory = result.category;
        this.currentTokenUsage = result.token_usage;
        this.turnHistory = result.turn_history;

        await this.reloadCategories();
        await this.reloadChats();
        await this.reloadChat();
    },

    async renameChat(chat_id, newTitle) {
        await simpleApiPost(`/api/chat/rename/${chat_id}`, {title: newTitle});
        await this.reloadChats();
    },

    async deleteChat(chat_id) {
        if (!confirm("Are you sure you want to delete this chat?")) { return }

        await simpleApiPost(`/api/chat/delete/${chat_id}`);
        await this.reloadChats();
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

    async reloadChat() {
        stream = Alpine.store("stream");

        if (!this.selectedChat) {
            console.log("tried to reload the chat, but no chat is loaded!");
            return;
        }

        const result = await simpleApiFetch(`/api/chat/current`);
        if (!result) { return }

        this.chat = result;
        this.selectedChat = result.id;
        // note: deliberately not touching selectedCategory here - a plain
        // data refresh must not change the category the user is looking at

        this.turnHistory = result.turn_history;
        Alpine.store('artifact').restoreFromHistory(this.selectedChat, this.turnHistory);
    },

    async reloadCategories() {
        this.categories = await simpleApiFetch('/api/chats/categories');
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
        stream = Alpine.store('stream');
        if (stream.state !== 'idle') {
            // don't allow sending during a stream
            // (TODO: allow nudging (interrupting the stream and sending a new message))
            return;
        }

        Alpine.store("stream").state = "message_sending";
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
            files: files
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
        if (window.socket) {
            window.socket.close();
        }
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
      if (!msg) return;
      navigator.clipboard.writeText(msg.content)
        .then(() => {
            return true;
        })
        .catch(err => {
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
