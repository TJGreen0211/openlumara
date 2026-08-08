const USERS_STORE = {
    users: [],
    loading: false,
    error: null,
    isAdmin: false,

    _getCsrfToken() {
        return document.querySelector('html').getAttribute('data-csrf-token') || '';
    },

    _csrfHeaders() {
        return {
            'Content-Type': 'application/json',
            'X-CSRF-Token': this._getCsrfToken()
        };
    },

    async init() {
        const role = document.querySelector('html').getAttribute('data-user-role');
        this.isAdmin = role === 'admin';
        if (this.isAdmin) {
            await this.loadUsers();
        }
    },

    async loadUsers() {
        this.loading = true;
        this.error = null;
        try {
            this.users = await simpleApiFetch('/api/users');
        } catch (err) {
            this.error = err.message || 'Failed to load users';
        } finally {
            this.loading = false;
        }
    },

    async createUser(username, password, role) {
        this.loading = true;
        this.error = null;
        try {
            await fetch('/api/users', {
                method: 'POST',
                headers: this._csrfHeaders(),
                body: JSON.stringify({ username, password, role })
            });
            await this.loadUsers();
        } catch (err) {
            this.error = err.message || 'Failed to create user';
        } finally {
            this.loading = false;
        }
    },

    async updateUser(username, changes) {
        this.loading = true;
        this.error = null;
        try {
            await fetch(`/api/users/${encodeURIComponent(username)}`, {
                method: 'PATCH',
                headers: this._csrfHeaders(),
                body: JSON.stringify(changes)
            });
            await this.loadUsers();
        } catch (err) {
            this.error = err.message || 'Failed to update user';
        } finally {
            this.loading = false;
        }
    },

    async deleteUser(username) {
        this.loading = true;
        this.error = null;
        try {
            await fetch(`/api/users/${encodeURIComponent(username)}`, {
                method: 'DELETE',
                headers: { 'X-CSRF-Token': this._getCsrfToken() }
            });
            await this.loadUsers();
        } catch (err) {
            this.error = err.message || 'Failed to delete user';
        } finally {
            this.loading = false;
        }
    }
};
