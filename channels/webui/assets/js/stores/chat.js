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

    user_input: '',
    last_user_input: '',

    currentTokenUsage: 0,

    async load() {
        // called by Alpine.init
        await this.reloadChats();
        await this.reloadCategories();

        const result = await simpleApiFetch(`/api/chat/current`);
        if (!result) { return }

        this.chat = result;
        this.selectedChat = result.id;
        this.selectedCategory = result.category;
        this.turnHistory = result.turn_history;
        this.currentTokenUsage = result.token_usage;

        // ensure the chat exists in the visible sidebar list before scrolling
        await this.ensureChatVisible(this.selectedChat);
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
        this.selectedCategory = result.category;

        this.turnHistory = result.turn_history;
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

        uploadStore.clear();
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
        this.editContent = msg.content;
        Alpine.store('ui').scrollToTurnIndex = turnIndex;
    },

    async cancelEdit() {
        this.editingMessageIndex = null;
        this.editContent = '';
    },

    async saveEdit(index) {
        await simpleSocketSend({
            "type": "message_edit",
            "index": index,
            "content": this.editContent
        });

        this.editingMessageIndex = null;
        this.editContent = '';
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
