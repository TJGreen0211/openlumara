/*
 * tiny IndexedDB store for meeting audio fragments (modelled on audio.js).
 *
 * one record per meeting: { id, codec, title, createdAt, lastSavedAt, fragments: [] }
 * where every fragment is a { tsMs, blob } pair - a raw MediaRecorder chunk
 * (timeslice ~15s). fragments append in insertion order so they can be played /
 * re-downloaded as parts. blobs are structured-cloneable, so the recorder chunks
 * land here verbatim. saved incrementally ondataavailable, so the audio survives
 * a crash mid-meeting even though the *transcript* (in memory) does not.
 */
const RecordingStore = {
    DB_NAME: "openlumara-meetings",
    STORE: "recordings",
    VERSION: 1,

    _dbPromise: null,

    // lazy single open; cached so repeated fragment saves reuse the connection
    _open() {
        if (this._dbPromise) {
            return this._dbPromise;
        }
        this._dbPromise = new Promise((resolve, reject) => {
            if (typeof indexedDB === "undefined") {
                reject(new Error("IndexedDB is not available in this browser."));
                return;
            }
            const req = indexedDB.open(this.DB_NAME, this.VERSION);
            req.onupgradeneeded = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains(this.STORE)) {
                    db.createObjectStore(this.STORE, { keyPath: "id" });
                }
            };
            req.onsuccess = (e) => resolve(e.target.result);
            req.onerror = (e) => reject(e.target.error || new Error("failed to open meeting db"));
            req.onblocked = () => reject(new Error("opening the meeting db was blocked"));
        });
        return this._dbPromise;
    },

    // upsert the meeting's record and append one fragment, preserving order
    async saveFragment(meetingId, frag) {
        const db = await this._open();
        return new Promise((resolve, reject) => {
            const tx = db.transaction([this.STORE], "readwrite");
            const store = tx.objectStore(this.STORE);
            const getReq = store.get(meetingId);
            getReq.onsuccess = (e) => {
                const rec = e.target.result || { id: meetingId, fragments: [], createdAt: Date.now() };
                rec.codec = frag.codec || rec.codec;
                rec.title = frag.title || rec.title;
                rec.lastSavedAt = frag.tsMs || Date.now();
                if (!Array.isArray(rec.fragments)) {
                    rec.fragments = [];
                }
                rec.fragments.push(frag);
                const putReq = store.put(rec);
                putReq.onsuccess = () => resolve(rec);
                putReq.onerror = () => reject(putReq.error);
            };
            getReq.onerror = () => reject(getReq.error);
        });
    },

    // whole meeting record (with its fragments) or null
    async getMeeting(meetingId) {
        const db = await this._open();
        return new Promise((resolve) => {
            const tx = db.transaction([this.STORE], "readonly");
            const req = tx.objectStore(this.STORE).get(meetingId);
            req.onsuccess = () => resolve(req.result || null);
            req.onerror = () => resolve(null);
        });
    },

    async deleteMeeting(meetingId) {
        const db = await this._open();
        return new Promise((resolve) => {
            const tx = db.transaction([this.STORE], "readwrite");
            const req = tx.objectStore(this.STORE).delete(meetingId);
            req.onsuccess = () => resolve(true);
            req.onerror = () => resolve(false);
        });
    }
};
