/*
 * live site preview store
 *
 * tracks which coder sandbox file the WebUI should render in the right-side
 * preview panel. file tool calls are streamed to the client *before* the
 * server executes them, so files created this turn don't exist on disk yet:
 * plain file_create/file_edit calls only *record* the artifact mid-stream,
 * and the panel opens on stream completion, once the writes have landed.
 * open_preview calls open it immediately, since by the time the model calls
 * it the file already exists. the iframe re-fetches on stream completion. no
 * cache-busting needed: the backend serves every artifact file with
 * `Cache-Control: no-store`.
 *
 * artifacts are scoped per-chat: `byChat` maps a chat id to the last HTML
 * entry point the AI touched in that chat. the header toggle and the panel
 * only ever show for the chat that actually has an artifact, so switching
 * away hides them and switching back restores them.
 *
 * the map is persisted to localStorage so the user can re-open a preview
 * after closing it (or after a page reload).
 */
const ARTIFACT_PERSIST_KEY = 'openlumara_artifact';
const ARTIFACT_PERSIST_VERSION = 2;

const ARTIFACT_STORE = {
    open: false,
    // chatId -> { sandbox, path }
    byChat: {},
    // the artifact recorded during the current stream ({ chatId, sandbox, path }
    // or null), so stream_complete can open the panel once the files the AI
    // wrote actually exist on disk
    pending: null,

    // the artifact (if any) belonging to the currently selected chat.
    // reading selectedChat here makes this getter reactive to chat switches.
    get current() {
        const chatId = Alpine.store('chat')?.selectedChat;
        return (chatId && this.byChat[chatId]) || null;
    },

    // called from init.js (before the websocket connects) so the per-chat map
    // is restored before any streamed tool call can persist a new entry.
    // stays closed so we never pop the panel open on a fresh page load.
    init() {
        try {
            const saved = JSON.parse(localStorage.getItem(ARTIFACT_PERSIST_KEY) || 'null');
            if (saved && saved.v === ARTIFACT_PERSIST_VERSION && saved.chats) {
                this.byChat = saved.chats;
            }
        } catch { /* ignore malformed / stale storage */ }
    },

    // Chat history is server-persisted, unlike localStorage. Rebuild this
    // chat's entry point from its saved coder calls after history is loaded.
    restoreFromHistory(chatId, turnHistory) {
        if (!chatId || !Array.isArray(turnHistory)) return;

        let latest = null;
        for (const turn of turnHistory) {
            for (const message of Array.isArray(turn?.messages) ? turn.messages : []) {
                for (const toolCall of Array.isArray(message?.tool_calls) ? message.tool_calls : []) {
                    const name = toolCall?.function?.name;
                    if (!name || !/(?:^|_)(file_create|file_edit|open_preview)$/.test(name)) continue;

                    let args;
                    try {
                        const rawArgs = toolCall.function.arguments;
                        args = typeof rawArgs === 'string' ? JSON.parse(rawArgs) : rawArgs;
                    } catch {
                        continue;
                    }

                    if (typeof args?.sandbox !== 'string' || !args.sandbox
                        || typeof args.path !== 'string' || !/\.html?$/i.test(args.path)) {
                        continue;
                    }
                    latest = { sandbox: args.sandbox, path: args.path };
                }
            }
        }

        if (!latest) return;
        this.byChat[chatId] = latest;
        this._persist();
    },

    _persist() {
        try {
            localStorage.setItem(ARTIFACT_PERSIST_KEY, JSON.stringify({
                v: ARTIFACT_PERSIST_VERSION,
                chats: this.byChat
            }));
        } catch { /* ignore quota / private-mode errors */ }
    },

    // percent-encode each segment (not the whole string) so FastAPI's
    // {path:path} still receives real slashes
    get entryUrl() {
        const cur = this.current;
        if (!cur) return null;
        return `/api/artifact/${encodeURIComponent(cur.sandbox)}/${cur.path.split('/').map(encodeURIComponent).join('/')}`;
    },

    get title() {
        return this.current ? this.current.path.split('/').pop() : 'Preview';
    },

    // record the artifact for the currently selected chat without opening the
    // panel. only real HTML entry points are tracked (case-insensitive); the
    // artifact is tied to whichever chat is active when the stream emits the
    // tool call.
    recordPreview(sandbox, path) {
        if (!path || !/\.html?$/i.test(path)) return;
        const chatId = Alpine.store('chat')?.selectedChat;
        if (!chatId) return;

        this.byChat[chatId] = { sandbox, path };
        this.pending = { chatId, sandbox, path };
        this._persist();
    },

    // file tools (file_create/file_edit) stream before their writes land on
    // disk, so just record - the panel opens on finalize().
    onFileTouched(sandbox, path) {
        this.recordPreview(sandbox, path);
    },

    // open_preview implies the file already exists, so open the panel right away
    onOpenPreview(sandbox, path) {
        this.recordPreview(sandbox, path);
        if (this.pending) this.open = true;
    },

    // runs on stream completion: the turn's writes have landed, so a recorded
    // (but not yet open) preview can safely be shown now.
    finalize() {
        if (!this.open && this.pending
            && this.pending.chatId === Alpine.store('chat')?.selectedChat) {
            this.open = true;
        }
        this.pending = null;
        if (this.open) this.refresh();
    },

    // the no-store header means re-asserting the iframe src is enough,
    // but an explicit reload() covers an already-mounted iframe
    refresh() {
        if (!this.open || !this.entryUrl) return;
        const iframe = document.getElementById('preview-iframe');
        if (iframe) iframe.contentWindow?.location.reload();
    },

    // a manual close is respected across the stream boundary: clearing pending
    // means finalize() won't pop the panel back open.
    close() { this.open = false; this.pending = null; },

    toggle() {
        if (!this.current) return;
        if (this.open) this.close();
        else this.open = true;
    },

    openInNewTab() { if (this.entryUrl) window.open(this.entryUrl, '_blank'); },

    // download the artifact. the `download` attribute is honored for
    // same-origin URLs (this is one), so no backend change is needed.
    download() {
        if (!this.entryUrl) return;
        const a = document.createElement('a');
        a.href = this.entryUrl;
        a.download = this.title;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }
};
