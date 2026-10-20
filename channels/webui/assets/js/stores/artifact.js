/*
 * live site preview store
 *
 * tracks the sites the AI builds in coder sandboxes, so the WebUI can render
 * them in the right-side preview panel. a chat can hold several sites
 * (separate prototypes), and each site can have several pages.
 *
 * a "site" is the folder holding its entry HTML file (e.g. `bakery/` for
 * `bakery/index.html`, or the sandbox root). HTML files touched later in that
 * folder - or below it - become pages of the same site. a site's user media
 * lives in `<folder>/media/` and is managed from the panel's media tab.
 *
 * file tool calls are streamed to the client *before* the server executes
 * them, so files created this turn don't exist on disk yet: plain
 * file_create/file_edit calls only *record* the site mid-stream, and the panel
 * opens on stream completion, once the writes have landed. open_preview calls
 * open it immediately, since by the time the model calls it the file already
 * exists. no cache-busting needed: the backend serves every artifact file with
 * `Cache-Control: no-store`. file urls carry a signed per-site token instead
 * of relying on the session cookie (see _token()).
 *
 * sites are scoped per-chat: `byChat` maps a chat id to { sites, active, page }.
 * `sites` is ordered by last touch (most recent last). the header toggle and
 * the panel only ever show for a chat that actually has a site.
 *
 * the map is persisted to localStorage so the user's picked site/page survives
 * a reload; the sites themselves are rebuilt from chat history on load.
 */
const ARTIFACT_PERSIST_KEY = 'openlumara_artifact';
const ARTIFACT_PERSIST_VERSION = 3;

const ARTIFACT_STORE = {
    open: false,
    // 'preview' | 'media'
    tab: 'preview',
    // chatId -> { sites: [{ sandbox, folder, entry, explicit, pages: [path] }], active: key, page: path }
    byChat: {},
    // the chat a site was recorded for during the current stream ({ chatId }
    // or null), so stream_complete can open the panel once the files the AI
    // wrote exist
    pending: null,
    // tool calls already recorded during the current stream
    _seen: new Set(),
    // site key -> signed url token (see _token()); not persisted, they expire
    tokens: {},
    _tokenRequests: new Set(),

    // media tab
    media: [],
    mediaLoading: false,
    mediaError: '',
    mediaBusy: false,
    importUrl: '',
    // whether image/video chat attachments are also saved into the open site
    saveAttachments: true,

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

    _persist() {
        try {
            localStorage.setItem(ARTIFACT_PERSIST_KEY, JSON.stringify({
                v: ARTIFACT_PERSIST_VERSION,
                chats: this.byChat
            }));
        } catch { /* ignore quota / private-mode errors */ }
    },

    /* ----------------------
     * site bookkeeping
     * ---------------------- */
    _key(site) { return `${site.sandbox}:${site.folder}`; },

    _dirname(path) {
        const i = path.lastIndexOf('/');
        return i === -1 ? '' : path.slice(0, i);
    },

    // the coder accepts paths with or without the sandbox prefix, and with either slash
    _normalizePath(sandbox, path) {
        let p = String(path).replace(/\\/g, '/').replace(/^(\.\/)+/, '').replace(/^\/+/, '');
        if (p.startsWith(`${sandbox}/`)) p = p.slice(sandbox.length + 1);
        return p;
    },

    _chatEntry(chatId) {
        if (!this.byChat[chatId]) this.byChat[chatId] = { sites: [], active: null, page: null };
        return this.byChat[chatId];
    },

    // records an HTML file the AI touched (or opened) into the chat's sites,
    // makes its site active and shows that page. returns the site.
    _record(entry, sandbox, rawPath, explicit) {
        const path = this._normalizePath(sandbox, rawPath);
        const folder = this._dirname(path);

        // the deepest existing site whose folder holds this file. the sandbox
        // root site only claims files directly in the root, so one stray
        // root index.html doesn't swallow every other site
        let site = null;
        for (const s of entry.sites) {
            if (s.sandbox !== sandbox) continue;
            const holds = s.folder === folder
                || (s.folder !== '' && folder.startsWith(`${s.folder}/`));
            if (holds && (!site || s.folder.length > site.folder.length)) site = s;
        }

        // an opened entry above existing sites absorbs them (e.g. `shop/blog/post.html`
        // was touched before `shop/index.html` was opened)
        if (explicit && (!site || site.folder !== folder) && folder !== '') {
            const absorbed = entry.sites.filter(s => s.sandbox === sandbox && s.folder.startsWith(`${folder}/`));
            if (absorbed.length) {
                entry.sites = entry.sites.filter(s => !absorbed.includes(s));
                site = {
                    sandbox, folder, entry: path, explicit: true,
                    pages: [...new Set(absorbed.flatMap(s => s.pages))]
                };
                entry.sites.push(site);
            }
        }

        if (!site) {
            site = { sandbox, folder, entry: path, explicit: false, pages: [] };
            entry.sites.push(site);
        }

        if (!site.pages.includes(path)) site.pages.push(path);

        // open_preview names the entry outright; otherwise prefer the folder's index.html
        if (explicit) {
            site.entry = path;
            site.explicit = true;
        } else if (!site.explicit && /(^|\/)index\.html?$/i.test(path) && this._dirname(path) === site.folder) {
            site.entry = path;
        }

        // most recently touched site last
        entry.sites = entry.sites.filter(s => s !== site).concat(site);
        entry.active = this._key(site);
        entry.page = path;
        return site;
    },

    // Chat history is server-persisted, unlike localStorage. Rebuild this
    // chat's sites from its saved coder calls after history is loaded.
    restoreFromHistory(chatId, turnHistory) {
        if (!chatId || !Array.isArray(turnHistory)) return;

        const rebuilt = { sites: [], active: null, page: null };
        for (const turn of turnHistory) {
            for (const message of Array.isArray(turn?.messages) ? turn.messages : []) {
                for (const toolCall of Array.isArray(message?.tool_calls) ? message.tool_calls : []) {
                    const call = this._parseCall(toolCall);
                    if (call) this._record(rebuilt, call.sandbox, call.path, call.explicit);
                }
            }
        }

        if (!rebuilt.sites.length) return;

        // keep the user's own pick of site/page if it still exists
        const prev = this.byChat[chatId];
        const prevSite = prev && rebuilt.sites.find(s => this._key(s) === prev.active);
        if (prevSite) {
            rebuilt.active = prev.active;
            rebuilt.page = prevSite.pages.includes(prev.page) ? prev.page : prevSite.entry;
        }

        this.byChat[chatId] = rebuilt;
        this._persist();
    },

    // returns { sandbox, path, explicit } for a coder call on an HTML file, else null.
    // only complete argument JSON is accepted: mid-stream, a partially streamed
    // path ("about.htm" before the "l" arrives) would otherwise become a bogus page
    _parseCall(toolCall) {
        const name = toolCall?.function?.name;
        // tools are namespaced as {module}_{method} (e.g. coder_file_create,
        // coder_open_preview), so match the trailing method name, not the bare name
        if (!name || !/(?:^|_)(file_create|file_edit|open_preview)$/.test(name)) return null;

        let args;
        try {
            const rawArgs = toolCall.function.arguments;
            args = typeof rawArgs === 'string' ? JSON.parse(rawArgs) : rawArgs;
        } catch {
            return null;
        }

        if (typeof args?.sandbox !== 'string' || !args.sandbox
            || typeof args.path !== 'string' || !/\.html?$/i.test(args.path)) {
            return null;
        }
        return { sandbox: args.sandbox, path: args.path, explicit: /(?:^|_)open_preview$/.test(name) };
    },

    /* ----------------------
     * getters
     * ---------------------- */
    get _entry() {
        const chatId = Alpine.store('chat')?.selectedChat;
        return (chatId && this.byChat[chatId]) || null;
    },

    // the active site of the currently selected chat (or null).
    // reading selectedChat here makes this getter reactive to chat switches.
    get current() {
        const entry = this._entry;
        if (!entry?.sites?.length) return null;
        return entry.sites.find(s => this._key(s) === entry.active) || entry.sites[entry.sites.length - 1];
    },

    // most recently touched first
    get sites() {
        return [...(this._entry?.sites || [])].reverse();
    },

    get pages() {
        return this.current?.pages || [];
    },

    get currentPage() {
        const site = this.current;
        if (!site) return null;
        const page = this._entry.page;
        return site.pages.includes(page) ? page : site.entry;
    },

    get activeKey() {
        return this.current ? this._key(this.current) : '';
    },

    siteLabel(site) {
        if (!site) return 'Preview';
        return site.folder ? site.folder.split('/').pop() : site.sandbox;
    },

    // a page's path relative to its site folder, for the page picker
    pageLabel(path) {
        const folder = this.current?.folder;
        return folder && path.startsWith(`${folder}/`) ? path.slice(folder.length + 1) : path;
    },

    // the preview iframe has an opaque origin, so the site's own requests
    // (images, css, links) never carry the session cookie. site files are
    // served under a signed per-site token instead, which relative urls inside
    // the site inherit. returns null (and fetches it) when not known yet - the
    // url getters re-run once it lands.
    _token(site) {
        const key = this._key(site);
        if (this.tokens[key]) return this.tokens[key];
        if (!this._tokenRequests.has(key)) {
            this._tokenRequests.add(key);
            const params = new URLSearchParams({ sandbox: site.sandbox, folder: site.folder });
            simpleApiFetch(`/api/artifact-token?${params}`)
                .then(data => { this.tokens[key] = data.token; })
                .catch(() => {})
                .finally(() => this._tokenRequests.delete(key));
        }
        return null;
    },

    _fileUrl(site, path) {
        const token = this._token(site);
        if (!token) return null;
        // percent-encode each segment (not the whole string) so FastAPI's
        // {path:path} still receives real slashes
        return `/api/artifact/${token}/${encodeURIComponent(site.sandbox)}/${path.split('/').map(encodeURIComponent).join('/')}`;
    },

    get entryUrl() {
        const site = this.current;
        return site ? this._fileUrl(site, this.currentPage) : null;
    },

    get title() {
        return this.currentPage ? this.currentPage.split('/').pop() : 'Preview';
    },

    // what the chat composer sends along so attachments land in this site
    get attachTarget() {
        const site = this.current;
        return site && this.saveAttachments ? { sandbox: site.sandbox, folder: site.folder } : null;
    },

    /* ----------------------
     * streaming hooks
     * ---------------------- */

    // record a coder tool call for the currently selected chat. the site is
    // tied to whichever chat is active when the stream emits the tool call.
    // `key` identifies the call: the whole segment is re-sent on every stream
    // chunk, and each call must only be handled once (or a panel the user
    // closed mid-stream would pop back open on the next chunk)
    recordToolCall(toolCall, key) {
        if (this._seen.has(key)) return;
        const call = this._parseCall(toolCall);
        if (!call) return;
        const chatId = Alpine.store('chat')?.selectedChat;
        if (!chatId) return;

        this._seen.add(key);
        this._record(this._chatEntry(chatId), call.sandbox, call.path, call.explicit);
        this.pending = { chatId };
        this._persist();

        // open_preview implies the file already exists, so open the panel right
        // away. file tools stream before their writes land on disk, so those
        // only record - the panel opens on finalize()
        if (call.explicit) this.open = true;
    },

    // runs on stream completion: the turn's writes have landed, so a recorded
    // (but not yet open) preview can safely be shown now.
    finalize() {
        if (!this.open && this.pending
            && this.pending.chatId === Alpine.store('chat')?.selectedChat) {
            this.open = true;
        }
        this.pending = null;
        this._seen.clear();
        if (this.open) {
            this.refresh();
            if (this.tab === 'media') this.loadMedia();
        }
    },

    /* ----------------------
     * panel actions
     * ---------------------- */
    selectSite(key) {
        const entry = this._entry;
        const site = entry?.sites.find(s => this._key(s) === key);
        if (!site) return;
        entry.active = key;
        entry.page = site.entry;
        this._persist();
        if (this.tab === 'media') this.loadMedia();
    },

    selectPage(path) {
        const entry = this._entry;
        if (!entry || !this.pages.includes(path)) return;
        entry.page = path;
        this._persist();
    },

    setTab(tab) {
        this.tab = tab;
        if (tab === 'media') this.loadMedia();
    },

    // the iframe document has an opaque origin (the backend serves it with a
    // CSP sandbox), so contentWindow.location.reload() throws a SecurityError.
    // re-assigning src always navigates again, even to the same url
    refresh() {
        if (!this.open || !this.entryUrl) return;
        const iframe = document.getElementById('preview-iframe');
        if (iframe) iframe.src = this.entryUrl;
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

    // download the current page. the `download` attribute is honored for
    // same-origin URLs (this is one), so no backend change is needed.
    download() {
        if (!this.entryUrl) return;
        const a = document.createElement('a');
        a.href = this.entryUrl;
        a.download = this.title;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    },

    /* ----------------------
     * media tab
     * ---------------------- */
    mediaUrl(item) {
        const site = this.current;
        if (!site) return '';
        return this._fileUrl(site, site.folder ? `${site.folder}/${item.path}` : item.path) || '';
    },

    _csrfHeaders() {
        return { 'X-CSRF-Token': document.documentElement.getAttribute('data-csrf-token') || '' };
    },

    async _mediaRequest(url, options) {
        const response = await fetch(url, { ...options, headers: { ...this._csrfHeaders(), ...(options.headers || {}) } });
        const result = await response.json();
        if (!result.success) throw new Error(typeof result.data === 'string' ? result.data : 'Request failed');
        return result.data;
    },

    async loadMedia() {
        const site = this.current;
        if (!site) { this.media = []; return; }
        const key = this._key(site);

        this.mediaLoading = true;
        this.mediaError = '';
        try {
            const params = new URLSearchParams({ sandbox: site.sandbox, folder: site.folder });
            const items = await simpleApiFetch(`/api/site-media?${params}`);
            // ignore a late answer for a site the user already switched away from
            if (this.activeKey === key) this.media = items;
        } catch (e) {
            this.media = [];
            this.mediaError = String(e?.message || e);
        } finally {
            this.mediaLoading = false;
        }
    },

    _siteForm(extra = {}) {
        const site = this.current;
        const form = new FormData();
        form.append('sandbox', site.sandbox);
        form.append('folder', site.folder);
        for (const [k, v] of Object.entries(extra)) form.append(k, v);
        return form;
    },

    // shared busy/error/reload handling for every media mutation
    async _mutateMedia(fn) {
        if (!this.current || this.mediaBusy) return;
        this.mediaBusy = true;
        this.mediaError = '';
        let error = '';
        try {
            await fn();
        } catch (e) {
            error = String(e?.message || e);
        } finally {
            this.mediaBusy = false;
            // reload even after a failure (a multi-file upload can partly succeed),
            // then show the error - loadMedia() clears it
            await this.loadMedia();
            if (error) this.mediaError = error;
        }
    },

    async uploadMedia(fileList) {
        const files = [...(fileList || [])];
        if (!files.length) return;
        await this._mutateMedia(async () => {
            const form = this._siteForm();
            for (const file of files) form.append('files', file, file.name);
            const result = await this._mediaRequest('/api/site-media/upload', { method: 'POST', body: form });
            if (result.errors?.length) throw new Error(result.errors.join('\n'));
        });
    },

    // swaps a placeholder for a real file. the backend rewrites the site's
    // references to it, so the preview just needs a reload
    async replacePlaceholder(name, file) {
        if (!file) return;
        await this._mutateMedia(async () => {
            const form = this._siteForm({ placeholder: name });
            form.append('file', file, file.name);
            await this._mediaRequest('/api/site-media/replace', { method: 'POST', body: form });
            this.refresh();
        });
    },

    async importFromUrl() {
        const url = this.importUrl.trim();
        if (!url) return;
        await this._mutateMedia(async () => {
            const site = this.current;
            await this._mediaRequest('/api/site-media/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sandbox: site.sandbox, folder: site.folder, url })
            });
            this.importUrl = '';
        });
    },

    async deleteMedia(name) {
        if (!confirm(`Delete ${name}? Pages that use it will show a broken image.`)) return;
        await this._mutateMedia(async () => {
            const site = this.current;
            await this._mediaRequest('/api/site-media/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sandbox: site.sandbox, folder: site.folder, name })
            });
        });
    },

    // drops a file's site-relative path into the composer so the user can
    // tell the AI where to use it
    insertIntoChat(item) {
        const chat = Alpine.store('chat');
        const sep = chat.user_input && !/\s$/.test(chat.user_input) ? ' ' : '';
        chat.user_input = `${chat.user_input}${sep}\`${item.path}\` `;
        document.getElementById('user-input')?.focus();
    }
};
