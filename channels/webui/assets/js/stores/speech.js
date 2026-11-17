/*
 * reading replies aloud (text-to-speech)
 *
 * - "button": a speaker button on each assistant reply reads it on demand
 * - "auto": additionally, a reply to a message you sent by voice (dictated, not
 *   edited afterwards) is read out as soon as it finishes
 *
 * the server turns a reply into a "plan": the text cleaned for speech (no code,
 * tables, markdown symbols or urls) split into runs of one language. each run is
 * streamed from the TTS server as raw PCM and scheduled on the audio clock as it
 * arrives, every block starting exactly where the previous one ends - so playback
 * starts after the first generated chunk (well under a second) and has no gaps as
 * long as the server generates faster than real time.
 *
 * playback goes through a Web Audio context that is unlocked during a user
 * gesture (sending, the mic button, the speaker button). that's what lets an
 * auto-spoken reply play later, with no gesture of its own, on mobile too.
 */
const SPEECH_STORE = {
    playingKey: null,        // what is being read: a turn index, or null
    loading: false,          // waiting for the first piece's audio
    error: null,

    _ctx: null,              // the playback AudioContext
    _sources: new Set(),     // scheduled blocks not yet finished
    _cursor: 0,              // audio-clock time where the next block starts
    _reader: null,           // the stream being read (cancelled by stop())
    _gen: 0,                 // bumps on every start/stop; stale loops check it and bail
    _autoPending: false,     // the message just sent was dictated: read its reply
    _turnsAtSend: 0,         // turn count when it was sent
    _wasStreaming: false,

    init() {
        // when a reply finishes streaming, read it if its message was dictated
        Alpine.effect(() => {
            const state = Alpine.store("stream").state;
            const idle = state === "idle";
            if (!idle) {
                this._wasStreaming = true;
                return;
            }
            if (this._wasStreaming) {
                this._wasStreaming = false;
                if (this._autoPending) {
                    this._autoPending = false;
                    // let the finished reply land in the turn history first
                    setTimeout(() => this._speakLatestReply(), 400);
                }
            }
        });
    },

    /* --------------------------- settings ----------------------------- */
    mode() {
        const s = Alpine.store("settings").settings || {};
        const webui = s.channels && s.channels.settings && s.channels.settings.webui;
        if (!webui || !String(webui.tts_server_url || "").trim()) {
            return "off";
        }
        return (s.api && s.api.voice_output) || "button";
    },

    available() {
        return this.mode() !== "off";
    },

    /* ------------------------ hooks from other stores ------------------ */
    // called from chat.send(), inside the send click / keypress
    noteSend(byVoice) {
        this.stop();
        this.unlock();
        this._autoPending = !!byVoice && this.mode() === "auto";
        // only a reply newer than this counts (a failed send must not read the previous one)
        this._turnsAtSend = (Alpine.store("chat").turnHistory || []).length;
    },

    // a gesture is happening: make sure playback is allowed later
    unlock() {
        try {
            if (!this._ctx) {
                const Ctx = window.AudioContext || window.webkitAudioContext;
                this._ctx = new Ctx();
            }
            if (this._ctx.state === "suspended") {
                this._ctx.resume().catch(() => {});
            }
        } catch (err) {
            this._ctx = null;
        }
        return this._ctx;
    },

    /* ----------------------------- playback ---------------------------- */
    isPlaying(key) {
        return this.playingKey === key;
    },

    // the speaker button: read this reply, or stop it if it's the one playing
    toggleTurn(turnIndex) {
        if (this.playingKey === turnIndex) {
            this.stop();
            return;
        }
        this.unlock();
        this.speakTurn(turnIndex);
    },

    speakTurn(turnIndex) {
        const chat = Alpine.store("chat");
        const turn = chat.turnHistory[turnIndex];
        const msg = turn && turn.messages && turn.messages[turn.messages.length - 1];
        const text = msg && typeof msg.content === "string" ? msg.content : "";
        if (text.trim()) {
            this.speak(text, turnIndex);
        }
    },

    _speakLatestReply() {
        const chat = Alpine.store("chat");
        const turns = chat.turnHistory || [];
        const i = turns.length - 1;
        if (i >= 0 && i >= this._turnsAtSend && turns[i].role === "assistant") {
            this.speakTurn(i);
        }
    },

    // a little audio is queued before the first block plays, so network jitter
    // between chunks doesn't turn into gaps
    LEAD_SEC: 0.15,
    // incoming bytes are scheduled in blocks of at least this much audio
    BLOCK_SEC: 0.1,

    async speak(text, key) {
        this.stop();
        const gen = ++this._gen;
        this.playingKey = key;
        this.loading = true;
        this.error = null;
        const ctx = this.unlock();
        try {
            if (!ctx) {
                throw new Error("audio playback is not available in this browser");
            }
            const plan = await simpleApiPost("/api/voice/speech_plan", { text });
            const pieces = (plan && plan.pieces) || [];
            // pieces are streamed one after another; the next request starts as soon as
            // the previous one has been generated, while its audio is still playing
            for (const piece of pieces) {
                if (gen !== this._gen) {
                    return;
                }
                await this._streamPiece(piece, gen);
            }
            await this._drained(gen);
        } catch (err) {
            if (gen === this._gen) {
                this.error = `Couldn't read the reply aloud: ${err.message || err}`;
                console.warn(this.error);
            }
        } finally {
            if (gen === this._gen) {
                this.playingKey = null;
                this.loading = false;
                this._reader = null;
            }
        }
    },

    async _streamPiece(piece, gen) {
        const resp = await fetch("/api/voice/speak", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: piece.text, language: piece.language })
        });
        const type = resp.headers.get("content-type") || "";
        if (!resp.ok || type.indexOf("audio") === -1 || !resp.body) {
            let detail = `HTTP ${resp.status}`;
            try {
                const raw = await resp.json();
                detail = raw.data || detail;
            } catch (e) {}
            throw new Error(detail);
        }
        const rate = parseInt(resp.headers.get("x-sample-rate") || "24000", 10);
        const reader = resp.body.getReader();
        this._reader = reader;

        const blockBytes = Math.max(2, Math.round(rate * this.BLOCK_SEC) * 2);
        let pending = new Uint8Array(0);
        const flush = (all) => {
            // whole 16-bit samples only; an odd trailing byte waits for the next read
            const usable = all ? pending.length - (pending.length % 2) : Math.floor(pending.length / blockBytes) * blockBytes;
            if (usable <= 0) {
                return;
            }
            this._schedule(pending.subarray(0, usable), rate, gen);
            pending = pending.slice(usable);
        };
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (gen !== this._gen) {
                    return;
                }
                if (done) {
                    break;
                }
                if (value && value.length) {
                    const joined = new Uint8Array(pending.length + value.length);
                    joined.set(pending);
                    joined.set(value, pending.length);
                    pending = joined;
                    flush(false);
                }
            }
            flush(true);
        } finally {
            if (this._reader === reader) {
                this._reader = null;
            }
        }
    },

    // little-endian 16-bit PCM -> an AudioBuffer scheduled right after the previous one
    _schedule(bytes, rate, gen) {
        if (gen !== this._gen || !this._ctx) {
            return;
        }
        const ctx = this._ctx;
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        const n = bytes.byteLength / 2;
        const buffer = ctx.createBuffer(1, n, rate);
        const data = buffer.getChannelData(0);
        for (let i = 0; i < n; i++) {
            data[i] = view.getInt16(i * 2, true) / 32768;
        }
        // starting fresh, or after the stream fell behind: begin a short lead from now
        const start = Math.max(this._cursor, ctx.currentTime + (this._sources.size ? 0.02 : this.LEAD_SEC));
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.destination);
        source.onended = () => this._sources.delete(source);
        this._sources.add(source);
        source.start(start);
        this._cursor = start + buffer.duration;
        this.loading = false;
    },

    // resolves once everything scheduled has played (or playback was stopped)
    _drained(gen) {
        return new Promise((resolve) => {
            const check = () => {
                if (gen !== this._gen || !this._ctx || this._ctx.currentTime >= this._cursor) {
                    resolve();
                    return;
                }
                setTimeout(check, Math.min(500, Math.max(50, (this._cursor - this._ctx.currentTime) * 1000)));
            };
            check();
        });
    },

    stop() {
        this._gen++;
        this._autoPending = false;
        // stop downloading (which also frees the TTS server) and silence what's queued
        if (this._reader) {
            try { this._reader.cancel(); } catch (e) {}
            this._reader = null;
        }
        for (const source of this._sources) {
            try { source.stop(); } catch (e) {}
        }
        this._sources.clear();
        this._cursor = 0;
        this.playingKey = null;
        this.loading = false;
    }
};
