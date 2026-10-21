UI_STORE = {
    scrollThreshold: 50,
    errors: [],
    currentModal: null,
    notice: null,
    // 'connected' | 'disconnected' - drives the status dot in the chat header
    connectionState: 'disconnected',

    shouldScroll: true,
    scrollToTurnIndex: null,

    windowWidth: window.innerWidth,
    isMobile: false,

    showSidebar: true,
    showCategories: true,
    showChatList: true,

    expandReasoning: true,

    async init() {
        // check if this is a phone
        this.windowWidth = window.innerWidth;
        this.isMobile = window.innerWidth <= 768;

        // hide sidebar on mobile (in favor of a hamburger button)
        this.showSidebar = !this.isMobile;

        // on mobile, the sidebar is a drill-down navigator rather than a two-column pane
        this.showCategories = !this.isMobile;
        this.showChatList = true;

        this.expandReasoning = localStorage.getItem("expandReasoning") !== 'false';
    },

    async toggleSidebar() {
        this.showSidebar = !this.showSidebar;
    },
    async toggleCategories() {
        this.showCategories = !this.showCategories;
    },
    async toggleChatList() {
        this.showChatList = !this.showChatList;
    },

    async toggleMobileSidebarView() {
        // toggles between the chatlist and the categories list
        if (this.showChatList) {
            this.showChatList = false;
            this.showCategories = true;
        } else {
            this.showChatList = true;
            this.showCategories = false;
        }
    },

    async openModal(name) {
        if (this.currentModal === 'settings') {
            // auto-save the settings when switching to a different modal
            await Alpine.store('settings').saveSettings();
        }

        this.currentModal = name;

        /*
         * post-open actions
         */

        if (name === 'logs') {
            // scroll down to the bottom
            await Alpine.nextTick();

            const el = document.getElementById('log-container');
            if (el) el.scrollTop = el.scrollHeight;
        }
    },
    async closeModal() {
        this.currentModal = null;
    },

    /*
     * Called from @scroll on the messages container.
     * Toggles shouldScroll based on whether the user is near the bottom.
     */
    onScroll(containerId = 'messages') {
        const el = document.getElementById(containerId);
        if (!el) return;

        const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        const wasAtBottom = this.shouldScroll;

        if (distFromBottom < this.scrollThreshold) {
            this.shouldScroll = true;
        } else {
            this.shouldScroll = false;
        }
    },

    async scrollToBottom(containerId = 'messages') {
        if (!this.shouldScroll) return;

        const el = document.getElementById(containerId);
        if (!el) return;

        Alpine.nextTick(() => {
            el.scrollTop = el.scrollHeight;
        });
    },

    async forceScrollToBottom(containerId = 'messages') {
        const el = document.getElementById(containerId);
        if (!el) return;

        Alpine.nextTick(() => {
            el.scrollTop = el.scrollHeight;
            this.shouldScroll = true;
        });
    },

    /*
     * Used by the scroll-to-bottom button. Setting scrollTop alone doesn't
     * stop an in-flight flick, so the view would drift back up after the
     * jump. Two kinds of momentum need stopping:
     *  - browser-driven flings (touch, iOS/macOS): overflow is turned off for
     *    a few frames, long enough for the compositor to see it and drop it
     *  - OS-driven inertia (Windows precision touchpads), which arrives as a
     *    stream of synthetic wheel events: those are swallowed until the
     *    stream pauses, so a fresh, deliberate scroll still works
     */
    jumpToBottom(containerId = 'messages') {
        const el = document.getElementById(containerId);
        if (!el) return;

        const toBottom = () => {
            el.scrollTop = el.scrollHeight;
            this.shouldScroll = true;
        };

        el.style.overflowY = 'hidden';
        toBottom();

        let idleTimer = null;
        const release = () => {
            el.removeEventListener('wheel', swallow, { capture: true });
        };
        const swallow = (event) => {
            event.preventDefault();
            clearTimeout(idleTimer);
            idleTimer = setTimeout(release, 150);
        };
        el.addEventListener('wheel', swallow, { capture: true, passive: false });
        idleTimer = setTimeout(release, 150);

        setTimeout(() => {
            el.style.overflowY = '';
            // content may have grown (streaming) while overflow was off
            toBottom();
        }, 100);
    },

    async scrollToTurn(turnIndex) {
        if (turnIndex === null || turnIndex === undefined) return;

        Alpine.nextTick(() => {
            const el = document.querySelector(`[data-turn-index="${turnIndex}"]`);
            if (!el) return;

            const container = document.getElementById('messages');
            if (!container) return;

            // Calculate scroll position to center the element
            const containerRect = container.getBoundingClientRect();
            const elementRect = el.getBoundingClientRect();
            const offset = elementRect.top - containerRect.top - (containerRect.height / 2) + (elementRect.height / 2);
            container.scrollTop += offset;

            this.scrollToTurnIndex = null;
        });
    }
}
