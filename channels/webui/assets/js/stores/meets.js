/*
 * meeting session store ("Record meeting" mode)
 *
 * sits the app open during a meeting and streams the auto-picked audio source
 * (screen/tab audio via getDisplayMedia when the browser offers it, else the
 * mic) through the EXISTING voice store (and its STT pipeline) into a
 * timestamped transcript, on stop sending that transcript as its own user
 * message into a dedicated chat.
 *
 * zero backend changes: everything here rides on voice.js (for the PCM / VAD /
 * commit pipeline), simpleSocketSend (new_chat + user_message over the
 * websocket), the chat store (switch/rename), RecordingStore (IndexedDB audio
 * retention) and plain fetch helpers.
 *
 * the voice pipeline is shared with normal dictation; this store OWNS the
 * MediaStream (feeding both the voice store's AudioContext and a parallel
 * MediaRecorder so the audio survives on disk), wires the meeting callbacks,
 * and owns the transcript / notes / completion-card state.
 */
const MEETS_STORE = {
    // --- visible state -------------------------------------------------
    active: false,
    finished: false,
    error: null,
    chatId: null,
    title: "",
    meetingId: null,              // id for the recorder + IndexedDB (a uuid, not the chat id)
    sensitivity: 3,               // 1 (least) .. 5 (most) VAD sensitivity
    segments: [],                 // [{ tsSec, text }] committed segments, in time order
    gaps: [],                     // [{ tsSec, n }] segments lost to a failed transcription
    markers: [],                  // [{ tsSec, durSec, kind }] pause / interruption entries
    wordCount: 0,
    paused: false,
    dismissed: false,             // completion card dismissed (state kept till reload)
    durationSec: 0,

    // --- private -------------------------------------------------------
    _rec: null,                   // the MediaRecorder (re-created on resume)
    _mime: "",                    // chosen recorder mime (reused on resume)
    _stream: null,                // the shared MediaStream
    _source: null,                // "screen" | "mic" - where _stream came from (auto-picked)
    _stopping: false,             // latch while stopMeeting runs (onended re-entrancy guard)
    _wakeLock: null,              // the WakeLockSentinel (best effort)
    _wakeHandler: null,           // visibilitychange listener (re-request wake lock)
    _tickTimer: null,             // 1s heartbeat (timer label + interruption check)
    _errTimer: null,
    _starting: false,             // latch while a meeting is launching (blocks a rapid 2nd click)
    _tabId: null,
    _startedAtWall: 0,
    _pausedWallMs: 0,
    _pausedAtWall: 0,
    _prevDivergence: 0,
    tick: 0,                      // reactive 1s counter (drives the timer label)
    _elapsedCache: 0,             // audio-clock seconds, refreshed once per tick
    stripKey: 0,                  // bumps per committed segment (re-triggers the fade)

    // the meets store sets voice.meetingVadAbsMin to one of these per slider step
    // (3 = the dictation default, so the slider's midpoint matches plain dictation)
    SENS: [0, 0.012, 0.008, 0.006, 0.0035, 0.002],
    MEETINGS_CATEGORY: "Meetings",   // finished meetings are filed here (created lazily by the backend)

    /* -------------------------- derived ----------------------------- */
    get lastSegmentText() {
        const s = this.segments[this.segments.length - 1];
        return s ? s.text : "";
    },

    // timer label: reads the 1s tick (the reactive dep) + a once-per-second
    // cached value, so it re-renders ~1/sec instead of on every audio chunk
    timerLabel() {
        void this.tick;
        const s = this.active ? this._elapsedCache : this.durationSec;
        return this._fmtClock(s);
    },

    durationLabel() {
        return this._fmtClock(this.durationSec);
    },

    _fmtClock(totalSec) { // 1:05:02 (or 5:02 under an hour)
        totalSec = Math.floor(totalSec || 0);
        const h = Math.floor(totalSec / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;
        if (h > 0) {
            return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
        }
        return `${m}:${String(s).padStart(2, "0")}`;
    },

    _fmtClock3(totalSec) { // 00:00:12 (HH:MM:SS) for transcript lines
        totalSec = Math.floor(totalSec || 0);
        const h = Math.floor(totalSec / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;
        return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    },

    setError(msg) {
        this.error = msg;
        if (this._errTimer) {
            clearTimeout(this._errTimer);
        }
        this._errTimer = setTimeout(() => {
            // only clear if we're still showing the same transient error
            this.error = null;
        }, 12000);
    },

    /* ------------------------ id / locks ----------------------------- */
    _tabIdent() {
        if (this._tabId) {
            return this._tabId;
        }
        try {
            let id = sessionStorage.getItem("ol_meeting_tab");
            if (!id) {
                id = (crypto && crypto.randomUUID) ? crypto.randomUUID() : "t" + Date.now() + Math.random();
                sessionStorage.setItem("ol_meeting_tab", id);
            }
            this._tabId = id;
        } catch (e) {
            this._tabId = "__no_session__" + Math.random();
        }
        return this._tabId;
    },

    _setLock() {
        try {
            localStorage.setItem("ol_meeting_lock", JSON.stringify({ tab: this._tabIdent(), ts: Date.now() }));
        } catch (e) {}
    },

    _clearLock() {
        try {
            const raw = localStorage.getItem("ol_meeting_lock");
            if (!raw) {
                return;
            }
            const lock = JSON.parse(raw);
            if (lock && lock.tab === this._tabIdent()) {
                localStorage.removeItem("ol_meeting_lock");
            }
        } catch (e) {
            try {
                localStorage.removeItem("ol_meeting_lock");
            } catch (e2) {}
        }
    },

    /* ------------------------ wake lock ------------------------------ */
    _requestWakeLock() {
        try {
            navigator.wakeLock.request("screen")
                .then((sentinel) => {
                    if (this.active) {
                        this._wakeLock = sentinel;
                    } else {
                        try { sentinel.release(); } catch (e) {}
                    }
                })
                .catch(() => {});
        } catch (e) {}
    },

    _releaseWakeLock() {
        if (this._wakeLock) {
            try { this._wakeLock.release(); } catch (e) {}
            this._wakeLock = null;
        }
        if (this._wakeHandler) {
            try { document.removeEventListener("visibilitychange", this._wakeHandler); } catch (e) {}
            this._wakeHandler = null;
        }
    },

    /* ------------------------ recorder ------------------------------- */
    _pickMime() {
        const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
        for (const c of candidates) {
            try {
                if (MediaRecorder.isTypeSupported(c)) {
                    return c;
                }
            } catch (e) {}
        }
        return "";
    },

    _onRecorderChunk(e) {
        if (e && e.data && e.data.size && this.meetingId) {
            RecordingStore.saveFragment(this.meetingId, {
                tsMs: Date.now(),
                blob: e.data,
                codec: this._mime,
                title: this.title
            }).catch((err) => console.warn("meeting audio save failed", err));
        }
    },

    _makeRecorder() {
        try {
            this._mime = this._pickMime();
            const opts = { audioBitsPerSecond: 64000 };
            if (this._mime) {
                opts.mimeType = this._mime;
            }
            const self = this;
            this._rec = new MediaRecorder(this._stream, opts);
            this._rec.ondataavailable = (e) => self._onRecorderChunk(e);
            this._rec.start(15000);
        } catch (err) {
            console.warn("meeting MediaRecorder failed to start", err);
        }
    },

    // stop the recorder and await its final dataavailable (the stop chunk) so the
    // last fragment is in IndexedDB before the shared stream is released
    _stopRecorder() {
        const rec = this._rec;
        this._rec = null;
        if (!rec || rec.state === "inactive") {
            return Promise.resolve();
        }
        return new Promise((resolve) => {
            let done = false;
            const finish = () => {
                if (!done) {
                    done = true;
                    resolve();
                }
            };
            rec.onstop = finish;
            rec.onerror = finish;
            try {
                rec.stop();
            } catch (e) {
                finish();
            }
            setTimeout(finish, 3000);
        });
    },

    /* ------------------------ heartbeat ------------------------------ */
    _heartbeat() {
        if (!this.active) {
            return;
        }
        this.tick += 1;
        this._elapsedCache = Alpine.store("voice").meetingElapsedSec() || 0;
        this._checkDivergence();
    },

    // if the tab was suspended while backgrounded, real (wall) time advanced but
    // the audio clock froze. when the tab comes forward the 1s tick resumes and
    // sees the accumulated jump - record it so the transcript isn't deceptive.
    _checkDivergence() {
        const voice = Alpine.store("voice");
        const wallEl = (Date.now() - this._startedAtWall - this._pausedWallMs) / 1000;
        let div = wallEl - (voice.meetingElapsedSec() || 0);
        if (div < 0) {
            div = 0;
        }
        const jump = div - this._prevDivergence;
        if (jump > 10) {
            this.markers.push({ tsSec: voice.meetingElapsedSec(), durSec: jump, kind: "interruption" });
        }
        this._prevDivergence = div;
    },

    _stopWatchers() {
        if (this._tickTimer) {
            clearInterval(this._tickTimer);
            this._tickTimer = null;
        }
    },

    /* ------------------------ audio source --------------------------- */
    // auto-pick the meeting's audio source: getDisplayMedia audio first (the
    // meeting is usually in another tab or app - that capture is cleaner than
    // the room mic), then the mic. cancelled pickers and browsers/platforms
    // without display-capture audio (iOS Safari, Win10 Chrome, ...) degrade to
    // the mic silently, so the button always does something. the mic runs
    // room-friendly constraints: echo cancellation + noise suppression are
    // tuned for solo dictation and can eat far-end speakers in a conference
    // room; raw audio goes straight to whisper, and the sensitivity slider
    // covers quiet speakers
    async _acquireMeetingStream() {
        const md = navigator.mediaDevices;
        if (typeof md.getDisplayMedia === "function") {
            let sys = null;
            try {
                sys = await md.getDisplayMedia({
                    audio: true, video: false, selfBrowserSurface: "exclude"
                });
            } catch (err) {
                sys = null;   // picker cancelled (or the OS refused) -> mic fallback
            }
            if (sys) {
                if (sys.getAudioTracks().length) {
                    return { stream: sys, source: "screen" };
                }
                // capture succeeded but the OS gave no audio -> release and use the mic
                try { sys.getTracks().forEach((t) => t.stop()); } catch (e) {}
            }
        }
        try {
            const mic = await md.getUserMedia({
                audio: { echoCancellation: false, noiseSuppression: false }
            });
            return { stream: mic, source: "mic" };
        } catch (err) {
            this.setError(err.name === "NotAllowedError"
                ? "Microphone access was denied. Please allow microphone access in the browser settings."
                : `Failed to start the meeting: ${err.message}`);
            return null;
        }
    },

    /* ------------------------ public API ----------------------------- */
    // thin latch wrapper: the sequence below is await-heavy, so between an early
    // click and `active` turning true a rapid second click would otherwise start a
    // second meeting (the voice.recording / cross-tab guards all lag the mic + chat
    // phase). setting _starting synchronously here is the only guard that catches it
    async startMeeting() {
        if (this.active || this._starting) {
            return;
        }
        this._starting = true;
        try {
            return await this._doStartMeeting();
        } finally {
            this._starting = false;
        }
    },

    async _doStartMeeting() {
        this.error = null;
        const voice = Alpine.store("voice");
        const chat = Alpine.store("chat");
        const streamStore = Alpine.store("stream");
        if (!voice.isSupported()) {
            this.setError("Voice input is not supported in this browser.");
            return;
        }
        if (voice.recording) {
            this.setError("The microphone is already in use.");
            return;
        }
        if (streamStore.state !== "idle") {
            this.setError("Wait for the current reply to finish before starting a meeting.");
            return;
        }

        // cross-tab guard: another tab with a fresh lock already owns the mic
        try {
            const raw = localStorage.getItem("ol_meeting_lock");
            if (raw) {
                const lock = JSON.parse(raw);
                if (lock && lock.tab !== this._tabIdent() && (Date.now() - lock.ts) < 5 * 60 * 1000) {
                    this.setError("A meeting is already being recorded in another tab. Stop it there first.");
                    return;
                }
            }
            this._setLock();
        } catch (e) { /* localStorage unavailable - skip the guard */ }

        // hard engine guard: meetings have no local fallback, so refuse to start
        // without a configured whisper server (global webui url wins, per-user next)
        const settings = Alpine.store("settings");
        const webui = settings && settings.settings && settings.settings.channels
            && settings.settings.channels.settings && settings.settings.channels.settings.webui;
        const api = settings && settings.settings && settings.settings.api;
        const serverUrl = (webui && webui.stt_whisper_server_url) || (api && api.voice_url) || "";
        if (!serverUrl) {
            this._clearLock();
            this.setError("No transcription server configured for meetings. Set the Voice URL in Settings -> Api, or stt_whisper_server_url in Settings -> Channels -> webui, then try again.");
            return;
        }

        // open the audio source once; the voice (PCM) and the MediaRecorder
        // (retention) share this single stream. the source is auto-picked:
        // screen/tab audio first (one native picker), mic as the fallback
        const acq = await this._acquireMeetingStream();
        if (!acq) {
            this._clearLock();
            return;
        }
        const stream = acq.stream;
        this._source = acq.source;
        this._stream = stream;

        // display capture can be ended mid-meeting from the browser chrome -
        // without this the meeting would sit "active" on a dead stream, so stop
        // it gracefully (the stop path flushes the recorder + transcript)
        if (this._source === "screen") {
            let ended = false;
            const self = this;
            stream.getAudioTracks().forEach((t) => {
                t.onended = () => {
                    if (ended || !self.active || self._stopping) {
                        return;
                    }
                    ended = true;
                    self.setError("Audio capture ended (screen sharing was stopped). The meeting was stopped.");
                    self.stopMeeting();
                };
            });
        }

        // reset per-meeting state + fresh id
        this.meetingId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : "m" + Date.now() + Math.random();
        this.segments = [];
        this.gaps = [];
        this.markers = [];
        this.wordCount = 0;
        this.paused = false;
        this.dismissed = false;
        this.durationSec = 0;
        this.tick = 0;
        this.stripKey = 0;
        this._pausedWallMs = 0;
        this._prevDivergence = 0;
        this._startedAtWall = Date.now();
        this.title = `Meeting ${this._fmtStamp()}`;

        // start retentive recording immediately so the whole meeting is captured
        // to IndexedDB, including any pre-roll before the new chat finishes
        this._makeRecorder();

        // dedicated chat for this meeting
        await this._createMeetingChat();

        // re-request the wake lock whenever the tab becomes visible again
        this._wakeHandler = () => {
            if (this.active && document.visibilityState === "visible") {
                this._requestWakeLock();
            }
        };
        document.addEventListener("visibilitychange", this._wakeHandler);
        this._requestWakeLock();

        // wire the voice pipeline to this meeting, then start it (sharing the stream)
        const self = this;
        voice.meetingVadAbsMin = this.SENS[this.sensitivity];
        voice.onSegmentCommitted = (seg) => self.addSegment(seg);
        voice.onGap = (g) => self.addGap(g);

        const ok = await voice.startRecording({ mode: "meeting", stream });
        if (!ok) {
            this._abort();
            this.setError(voice.error || "Failed to start the meeting recorder.");
            return;
        }

        this.active = true;
        this._tickTimer = setInterval(() => this._heartbeat(), 1000);
    },

    async _createMeetingChat() {
        const chat = Alpine.store("chat");
        const beforeId = (chat.chat && chat.chat.id) || chat.selectedChat || null;
        await simpleSocketSend({ type: "new_chat" });

        // chat_switched -> loadChat makes chat.id observable; give it ~3s to land
        const deadline = Date.now() + 3000;
        let newId = null;
        while (Date.now() < deadline) {
            const cur = (chat.chat && chat.chat.id) || chat.selectedChat || null;
            if (cur && cur !== beforeId) {
                newId = cur;
                break;
            }
            await new Promise((r) => setTimeout(r, 50));
        }
        if (!newId) {
            // the switch didn't land in time - fall back to whatever is selected now
            newId = (chat.chat && chat.chat.id) || chat.selectedChat || null;
        }
        this.chatId = newId;
        if (newId) {
            try { await chat.reloadChats(); } catch (e) {}
            try { await chat.ensureChatVisible(newId); } catch (e) {}
            try {
                await simpleApiPost(`/api/chat/rename/${newId}`, { title: this.title });
            } catch (e) {
                console.warn("meeting chat rename failed (continuing)", e);
            }
        }
    },

    addSegment(seg) {
        if (!this.active) {
            return;
        }
        this.segments.push({ tsSec: seg.tsSec, text: seg.text });
        this.wordCount = this._countWords();
        this.stripKey += 1;
    },

    addGap(g) {
        if (!this.active) {
            return;
        }
        this.gaps.push({ tsSec: g.tsSec, n: g.index });
    },

    _countWords() {
        let n = 0;
        for (const s of this.segments) {
            n += s.text.split(/\s+/).filter(Boolean).length;
        }
        return n;
    },

    setSensitivity(v) {
        const n = Math.min(5, Math.max(1, Math.round(Number(v) || 3)));
        this.sensitivity = n;
        Alpine.store("voice").meetingVadAbsMin = this.SENS[n];
    },

    async pause() {
        if (!this.active || this.paused) {
            return;
        }
        const voice = Alpine.store("voice");
        this.paused = true;
        this._pausedAtWall = Date.now();
        try { await voice.pauseCapture(); } catch (e) {}
        // the recorder can't be resumed, only stopped - flush its final chunk to IDB
        await this._stopRecorder();
    },

    async resume() {
        if (!this.active || !this.paused) {
            return;
        }
        const voice = Alpine.store("voice");
        // reopen the recorder on the (still alive) shared stream before resuming audio
        this._makeRecorder();
        try { await voice.resumeCapture(); } catch (e) {}
        const elapsed = Date.now() - this._pausedAtWall;
        this._pausedWallMs += elapsed;
        this.markers.push({ tsSec: voice.meetingElapsedSec(), durSec: elapsed / 1000, kind: "pause" });
        this.paused = false;
    },

    async stopMeeting() {
        // _stopping guards re-entry: releasing the stream fires the screen
        // capture's track onended handlers mid-stop, which would otherwise call
        // stopMeeting again while the first run is still in flight
        if (!this.active || this._stopping) {
            return;
        }
        this._stopping = true;
        try {
            const voice = Alpine.store("voice");
            this._stopWatchers();

            // 1. flush the recorder (final chunk lands in IDB) BEFORE the voice
            //    pipeline releases the shared stream (releaseAudio stops the tracks)
            await this._stopRecorder();

            // 2. drain the voice pipeline: waits the in-flight commit, commits the
            //    tail (emitted via onSegmentCommitted), and releases the audio
            await voice.stopMeetingRecording();

            this.active = false;
            this._releaseWakeLock();
            this._clearLock();
            if (this._stream) {
                try { this._stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
                this._stream = null;
            }
            this.durationSec = Math.round(voice.meetingElapsedSec());
            this.finished = true;

            // 3. send the transcript as its own user message into the meeting chat
            await this._sendTranscript();
        } finally {
            this._stopping = false;
            // once the meeting has ended, re-home its chat into the Meetings category.
            // runs even if the stop path threw part-way (the chat already exists and
            // carries the "Meeting ..." title); the helper is best-effort so it can
            // never surface an error here
            await this._fileInMeetings();
        }
    },

    // move the meeting's chat into the Meetings category and refresh the sidebar.
    // reuses the chat store's move helper (single POST + local category update +
    // reloads). clears draggedChatCategory first: it only guards against a
    // drag-and-drop re-drop into the same category, and a stale value left over from
    // an earlier drag would otherwise make the helper no-op. best-effort - a failure
    // is logged, never thrown
    async _fileInMeetings() {
        if (!this.chatId) {
            return;
        }
        const chat = Alpine.store("chat");
        try {
            chat.draggedChatCategory = null;
            await chat.moveChatToCategory(this.chatId, this.MEETINGS_CATEGORY);
        } catch (e) {
            console.warn("failed to file the meeting chat into the Meetings category", e);
        }
    },

    // assemble the plain-text transcript (segments + pause/interruption markers +
    // gap markers, ordered by time) and send it as a user message. using "--"
    // lines keeps it parseable as plain text, and it lands in the chat context so
    // "generate notes" can read it back for free
    async _sendTranscript() {
        const lines = [];
        lines.push(`Meeting transcript - ${this.title}`);
        lines.push(`Duration: ${this._fmtClock(this.durationSec)} - ${this.wordCount} words`);
        lines.push("");

        const events = [];
        for (const s of this.segments) {
            events.push({ t: s.tsSec, line: `[${this._fmtClock3(s.tsSec)}] ${s.text}` });
        }
        for (const g of this.gaps) {
            events.push({ t: g.tsSec, line: `[${this._fmtClock3(g.tsSec)}] -- segment missing (transcription failed; audio preserved locally) --` });
        }
        for (const m of this.markers) {
            if (m.kind === "pause") {
                events.push({ t: m.tsSec, line: `[${this._fmtClock3(m.tsSec)}] -- recording paused for ${this._fmtClock(m.durSec)} --` });
            } else if (m.kind === "interruption") {
                events.push({ t: m.tsSec, line: `[${this._fmtClock3(m.tsSec)}] -- recording interrupted ~${Math.round(m.durSec)}s (tab was backgrounded) --` });
            }
        }
        events.sort((a, b) => a.t - b.t);
        for (const e of events) {
            lines.push(e.line);
        }

        await simpleSocketSend({ type: "user_message", content: lines.join("\n") });
    },

    async generateNotes() {
        if (Alpine.store("stream").state !== "idle") {
            return;
        }
        await simpleSocketSend({
            type: "user_message",
            content: "Write meeting notes from the transcript in this chat. Structure: summary, decisions made, action items (name the owner if one was stated), open questions."
        });
    },

    async viewTranscript() {
        try {
            await Alpine.store("ui").forceScrollToBottom();
        } catch (e) {}
    },

    async downloadRecording() {
        if (!this.meetingId) {
            return;
        }
        let rec = null;
        try {
            rec = await RecordingStore.getMeeting(this.meetingId);
        } catch (e) {}
        if (!rec || !rec.fragments || !rec.fragments.length) {
            this.setError("No saved recording found for this meeting.");
            return;
        }
        const frags = rec.fragments;
        const c = rec.codec || "";
        const ext = c.indexOf("mp4") !== -1 ? "mp4" : "webm";
        const single = frags.length === 1;
        for (let i = 0; i < frags.length; i++) {
            // raw container segments don't concatenate cleanly, so multi-part
            // meetings download as N parts rather than one faked file
            const name = single
                ? `meeting-${this._fmtStampFile()}.${ext}`
                : `meeting-${this._fmtStampFile()}-part${i + 1}.${ext}`;
            this._triggerDownload(frags[i].blob, name);
            if (!single && i < frags.length - 1) {
                await new Promise((r) => setTimeout(r, 500));
            }
        }
    },

    _triggerDownload(blob, name) {
        try {
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = name;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(url), 4000);
        } catch (e) {
            console.warn("meeting download failed", e);
        }
    },

    dismissCard() {
        this.finished = false;
    },

    /* ------------------------ teardown ------------------------------- */
    _abort() {
        // called only when startMeeting fails part-way (e.g. voice.startRecording
        // couldn't bring up the AudioContext): release anything we opened
        this._stopWatchers();
        this._releaseWakeLock();
        this._stopRecorder();
        if (this._stream) {
            try { this._stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
            this._stream = null;
        }
        if (this.meetingId) {
            try { RecordingStore.deleteMeeting(this.meetingId); } catch (e) {}
            this.meetingId = null;
        }
        const voice = Alpine.store("voice");
        voice.onSegmentCommitted = null;
        voice.onGap = null;
        this._clearLock();
    },

    _fmtStamp() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },

    _fmtStampFile() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
    }
};

// clear this tab's meeting lock if it dies (tab close / crash) while a meeting
// was active, so a stale lock can't block a fresh one. ownership is checked via
// the per-tab id in sessionStorage, so closing the OTHER tab is unaffected.
(function clearMeetingLockOnGone() {
    const clearOwned = () => {
        try {
            const raw = localStorage.getItem("ol_meeting_lock");
            if (!raw) {
                return;
            }
            const lock = JSON.parse(raw);
            const tabId = sessionStorage.getItem("ol_meeting_tab");
            if (lock && tabId && lock.tab === tabId) {
                localStorage.removeItem("ol_meeting_lock");
            }
        } catch (e) {}
    };
    window.addEventListener("pagehide", clearOwned);
    window.addEventListener("beforeunload", clearOwned);
})();
