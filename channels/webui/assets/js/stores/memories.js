/*
 * the settings modal's memories tab: the logged-in user's long-term memories
 * (modules/memory.py), listed, searched, edited, pinned and forgotten
 */
const MEMORIES_STORE = {
    items: [],
    categories: [],
    modelNote: '',
    loading: false,
    error: null,

    // search: the text filter runs locally, the server adds matches by meaning
    query: '',
    searchIds: null,
    _searchTimer: null,
    _searchSeq: 0,

    newContent: '',

    editingId: null,
    editContent: '',
    editCategory: '',

    // the last forgotten memory, for undo
    forgotten: null,
    _forgottenTimer: null,

    get visible() {
        const q = this.query.trim().toLowerCase();
        if (!q) return this.items;
        const words = q.split(/\s+/);
        const local = this.items.filter(m => words.every(w => m.content.toLowerCase().includes(w)));
        if (!this.searchIds) return local;
        const shown = new Set(local.map(m => m.id));
        const byId = new Map(this.items.map(m => [m.id, m]));
        const extra = this.searchIds.map(id => byId.get(id)).filter(m => m && !shown.has(m.id));
        return [...local, ...extra];
    },

    async _send(method, url, body = null) {
        const response = await fetch(url, {
            method,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': document.documentElement.getAttribute('data-csrf-token') || ''
            },
            body: body === null ? null : JSON.stringify(body)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) {
            throw new Error(data.data || 'Request failed');
        }
        return data.data;
    },

    async _try(fn) {
        this.error = null;
        try {
            return await fn();
        } catch (err) {
            this.error = err.message || String(err);
        }
    },

    async load() {
        this.loading = this.items.length === 0;
        await this._try(async () => {
            const data = await simpleApiFetch('/api/memories');
            this.items = data.memories;
            this.categories = data.categories;
            this.modelNote = data.model_note;
        });
        this.loading = false;
    },

    onQuery() {
        clearTimeout(this._searchTimer);
        this.searchIds = null;
        const q = this.query.trim();
        if (!q) return;
        const seq = ++this._searchSeq;
        this._searchTimer = setTimeout(async () => {
            try {
                const ids = await simpleApiFetch('/api/memories/search?q=' + encodeURIComponent(q));
                // drop answers to an older query
                if (seq === this._searchSeq) this.searchIds = ids;
            } catch (err) {
                // the text filter still works; meaning search is a bonus
                console.warn('memory search failed:', err);
            }
        }, 300);
    },

    async add() {
        const content = this.newContent.trim();
        if (!content) return;
        await this._try(async () => {
            await this._send('POST', '/api/memories', { content });
            this.newContent = '';
            await this.load();
        });
    },

    startEdit(memory) {
        this.editingId = memory.id;
        this.editContent = memory.content;
        this.editCategory = memory.category;
    },

    cancelEdit() {
        this.editingId = null;
    },

    async saveEdit() {
        const memory = this.items.find(m => m.id === this.editingId);
        const content = this.editContent.trim();
        if (!memory || !content) return this.cancelEdit();
        const changes = {};
        if (content !== memory.content) changes.content = content;
        if (this.editCategory !== memory.category) changes.category = this.editCategory;
        if (Object.keys(changes).length) {
            await this._try(async () => {
                Object.assign(memory, await this._send('PATCH', `/api/memories/${encodeURIComponent(memory.id)}`, changes));
            });
        }
        this.editingId = null;
    },

    async togglePin(memory) {
        await this._try(async () => {
            await this._send('PATCH', `/api/memories/${encodeURIComponent(memory.id)}`, { pinned: !memory.pinned });
            // pinned memories are listed first
            await this.load();
        });
    },

    async forget(memory) {
        await this._try(async () => {
            await this._send('DELETE', `/api/memories/${encodeURIComponent(memory.id)}`);
            this.items = this.items.filter(m => m.id !== memory.id);
            this.forgotten = memory;
            clearTimeout(this._forgottenTimer);
            this._forgottenTimer = setTimeout(() => { this.forgotten = null; }, 8000);
        });
    },

    async undo() {
        const memory = this.forgotten;
        if (!memory) return;
        this.forgotten = null;
        clearTimeout(this._forgottenTimer);
        await this._try(async () => {
            await this._send('POST', `/api/memories/${encodeURIComponent(memory.id)}/restore`);
            await this.load();
        });
    }
};
