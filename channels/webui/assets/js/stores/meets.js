/*
 * meeting session store ("Record meeting" mode)
 *
 * sits the app open during a meeting and streams the auto-picked audio source
 * (screen/tab audio via getDisplayMedia when the browser offers it, else the
 * mic) through the EXISTING voice store (and its STT pipeline) into a
 * timestamped transcript, on stop sending that transcript with meeting-notes
 * instructions into a dedicated chat.
 *
 * zero backend changes: everything here rides on voice.js (for the PCM / VAD /
 * commit pipeline), the chat store (category-aware chat creation), simpleSocketSend
 * (user_message over the websocket), RecordingStore (IndexedDB audio retention)
 * and plain fetch helpers.
 *
 * the voice pipeline is shared with normal dictation; this store OWNS the
 * MediaStream (feeding both the voice store's AudioContext and a parallel
 * MediaRecorder so the audio survives on disk), wires the meeting callbacks,
 * and owns the transcript / notes / completion-card state.
 */
const MEETS_STORE = {
    // --- visible state -------------------------------------------------
    active: false,
    stopping: false,
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
    diarizing: false,             // the finished recording is being re-transcribed with speaker labels
    diarized: false,              // segments came from the diarized recording (they carry .speaker)
    attendees: "",                // who's in the meeting, as typed (comma-separated); saved per meeting room
    attendeesOpen: false,         // the attendee field is expanded

    // --- private -------------------------------------------------------
    _rec: null,                   // the MediaRecorder (re-created on resume)
    _mime: "",                    // chosen recorder mime (reused on resume)
    _recPart: 0,                  // recorder session number (1 + one per resume); tags each fragment
    _recStartMs: 0,               // wall time the first recorder started
    _preRollSec: 0,               // recording audio before the transcript clock started
    _sessionId: null,             // speech-server meeting session the recording streams to (diarization on)
    _notesLabelled: false,        // whether the last transcript sent had voice labels (for "Regenerate notes")
    _sessionBroken: false,        // streaming failed; the whole recording is uploaded at stop instead
    _upload: null,                // promise chain: recorder chunks are sent strictly in order
    _partSeq: {},                 // part -> number of the last piece queued (pieces are numbered from 0)
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
    MEETINGS_CATEGORY: "Meetings",
    // meeting-notes instructions, sent after the transcript. built by _notesInstructions():
    // the speaker paragraph depends on whether the transcript is labelled by voice
    NOTES_SPEAKERS_LABELLED: [
        "Speakers",
        "- Work out who each speaker is before writing anything else. Look for: someone introducing themselves, "
            + "being addressed by name (\"thanks, Ana\"), answering when a name is called, being asked by name to do "
            + "something and agreeing, or stating their role (\"as the project lead...\").",
        "- Give your best identification for every label with the evidence and how sure you are, e.g. "
            + "\"Speaker 2: Ana, project lead (addressed by name at 00:04:10)\" or "
            + "\"Speaker 3: probably Luis (answers when Luis is asked, 00:31:02)\". With no evidence at all, write "
            + "\"unknown\". A guess marked \"probably\" is fine here; never present an unsupported guess as fact.",
        "- Use those names everywhere else: \"Ana (Speaker 2)\" the first time, then \"Ana\".",
        "- Voice labels are occasionally wrong. If one person clearly appears under two labels (the same name, or "
            + "one label answers questions addressed to the other), say so in Attendees and treat them as one person.",
    ],
    NOTES_SPEAKERS_UNLABELLED: [
        "Speakers",
        "- The transcript has no speaker labels. Still work out who said what from the conversation itself: names "
            + "used, who answers whom, \"I'll...\" right after someone is asked by name.",
        "- Attribute statements and action items to a person only when the conversation supports it, and list "
            + "everyone mentioned as taking part under Attendees.",
    ],
    // added when the user listed attendees
    NOTES_ATTENDEES_LABELLED: [
        "- The user listed who attended (see Attendees at the top). Match each speaker label to one of those "
            + "names: evidence from the conversation first, then elimination. Mark a match made only by "
            + "elimination \"(by elimination)\".",
        "- Not everyone listed has to have spoken, and someone not listed may have. Leave a label \"unknown\" "
            + "rather than forcing a listed name onto it.",
        "- Spell names as the user listed them, even where the transcript misspells them.",
    ],
    NOTES_ATTENDEES_UNLABELLED: [
        "- The user listed who attended (see Attendees at the top). Use those names for the people in the "
            + "meeting, spelled as listed, even where the transcript misspells them.",
    ],
    NOTES_BODY: [
        "Action items (the most important part)",
        "- List every task someone committed to or was given: \"I'll...\", \"I can take that\", "
            + "\"can you...?\" followed by agreement, \"X will...\", \"let's have X...\". Read the whole "
            + "transcript for them, including ones mentioned in passing.",
        "- Owner: the person who will do it - whoever committed, or whoever it was assigned to - by name if "
            + "identified, otherwise their speaker label. \"Unassigned\" if the group agreed it should happen but "
            + "nobody took it.",
        "- Due: as stated, with relative dates worked out from the meeting date (\"Friday 20 Nov\"); \"-\" if "
            + "none was given. Don't invent deadlines.",
        "- A suggestion nobody agreed to is not an action item; put it under Open questions.",
        "",
        "Accuracy",
        "- Include only what was said. Don't add advice, background, or conclusions the participants didn't "
            + "reach. Something proposed but not agreed is an open question, not a decision.",
        "- The transcript comes from speech recognition. Fix obvious mis-hearings silently; if a name, number, "
            + "date or amount is unclear, write it as heard followed by [unclear].",
        "- \"recording paused\", \"recording interrupted\" and \"segment missing\" lines mean part of the meeting "
            + "is not in the transcript. If a topic is cut off there, say so.",
        "",
        "Language",
        "- Write the notes in the language most of the meeting was held in. Include what was said in any other "
            + "language too, translated, marked e.g. \"(in Spanish)\". Keep names and terms as spoken.",
        "- Write as a neutral note-taker.",
        "",
        "Format (Markdown), in this order:",
        "## Attendees - each speaker label with name, role and evidence, as above.",
        "## Summary - 3 to 6 sentences: the purpose of the meeting and its main outcomes.",
        "## Action items - a table: Owner | Task | Due | Agreed at (the transcript time). "
            + "Write \"None recorded.\" if there are none.",
        "## Decisions - what was decided, and by whom if stated.",
        "## Discussion - one short subsection per topic in the order discussed, with its start time, the key "
            + "points, and who made them.",
        "## Open questions - unresolved issues and follow-ups.",
        "Skip an empty section, except Action items. A longer meeting gets more detail in Discussion, not a "
            + "longer Summary.",
    ],
    // consecutive lines from one speaker are merged into one transcript line, up to this long.
    // short, so the times the notes cite (e.g. when an action item was agreed) stay close
    MERGE_LINE_SEC: 30,

    /* -------------------------- derived ----------------------------- */
    get attendeeCount() {
        return this.attendeeList().length;
    },

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

    _onRecorderChunk(e, part) {
        if (e && e.data && e.data.size && this.meetingId) {
            RecordingStore.saveFragment(this.meetingId, {
                tsMs: Date.now(),
                part,
                blob: e.data,
                codec: this._mime,
                title: this.title
            }).catch((err) => console.warn("meeting audio save failed", err));
            this._queueUpload(e.data, part);
        }
    },

    /* ------------------- streaming to the speech server -------------- */
    // with speaker labelling on, the recording streams to the speech server while the
    // meeting runs, which transcribes it window by window - so stopping only leaves the
    // last few minutes to process. any failure here just falls back to uploading the
    // whole saved recording at stop, so nothing is ever lost.
    _openSession() {
        this._sessionId = null;
        this._partSeq = {};
        this._sessionBroken = !this._diarizeEnabled();
        if (this._sessionBroken) {
            return Promise.resolve();
        }
        // a listed attendee count caps how many voices the server may find (a maximum,
        // not an exact count: someone listed may not speak)
        const listed = this.attendeeList().length;
        const meetingId = this.meetingId;
        return simpleApiPost("/api/meeting/session/start", listed ? { max_speakers: Math.max(2, listed) } : {})
            .then((data) => {
                if (meetingId !== this.meetingId) {
                    // the meeting was aborted while the session was opening: release it
                    simpleApiPost(`/api/meeting/session/${data.id}/discard`, {}).catch(() => {});
                    return;
                }
                this._sessionId = data.id;
            })
            .catch((err) => {
                console.warn("meeting session could not start; the recording will be uploaded at the end", err);
                this._sessionBroken = true;
            });
    },

    _queueUpload(blob, part) {
        if (this._sessionBroken || !this._upload) {
            return;
        }
        const meetingId = this.meetingId;
        const ext = (this._mime || "").indexOf("mp4") !== -1 ? "mp4" : "webm";
        // pieces are numbered per part (0, 1, 2, ...), so a retry of a piece the server already
        // received - its reply was lost - is recognised and not appended twice
        const seq = this._partSeq[part] = (this._partSeq[part] === undefined ? -1 : this._partSeq[part]) + 1;
        this._upload = this._upload.then(async () => {
            // chunks only decode as one continuous stream, so a lost chunk can't be
            // skipped: retry it, and if it still fails stop streaming for good
            if (this._sessionBroken || meetingId !== this.meetingId) {
                return;
            }
            if (!this._sessionId) {
                this._sessionBroken = true;
                return;
            }
            let lastErr = null;
            for (let attempt = 1; attempt <= 3; attempt++) {
                try {
                    const form = new FormData();
                    form.append("part", String(part));
                    form.append("seq", String(seq));
                    form.append("file", blob, `part${part}.${ext}`);
                    const raw = await (await fetch(`/api/meeting/session/${this._sessionId}/audio`,
                        { method: "POST", body: form })).json();
                    if (raw.success) {
                        return;
                    }
                    lastErr = raw.data;
                } catch (err) {
                    lastErr = err;
                }
                await new Promise((r) => setTimeout(r, 1500 * attempt));
            }
            console.warn("meeting streaming failed; the recording will be uploaded at the end", lastErr);
            this._sessionBroken = true;
        });
    },

    _discardSession() {
        const id = this._sessionId;
        this._sessionId = null;
        this._sessionBroken = true;
        if (id) {
            simpleApiPost(`/api/meeting/session/${id}/discard`, {}).catch(() => {});
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
            // timeslice chunks only play back joined to the rest of their session,
            // so every chunk records which recorder session it belongs to
            const part = ++this._recPart;
            if (part === 1) {
                this._recStartMs = Date.now();
            }
            this._rec = new MediaRecorder(this._stream, opts);
            this._rec.ondataavailable = (e) => self._onRecorderChunk(e, part);
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
    async startMeeting(opts = {}) {
        // stopping = the previous meeting is still flushing its transcript;
        // starting now could land the new boundary BEFORE that transcript
        if (this.active || this.stopping || this._starting) {
            return;
        }
        this._starting = true;
        try {
            return await this._doStartMeeting(opts.targetChatId || null);
        } finally {
            this._starting = false;
        }
    },

    async _doStartMeeting(targetChatId = null) {
        this.error = null;
        this.stopping = false;
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
        if (targetChatId && chat.selectedChat !== targetChatId) {
            // in-chat meetings only ever start from the room's own UI, so the
            // room must be the open chat - never record into a random chat
            this.setError("Open the meeting room chat first to start a meeting in it.");
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

        // prime the AudioContext NOW: everything above this line is
        // synchronous, so this tap's user gesture is still live. iOS Safari
        // starts audio only from a context created in-gesture, and the mic
        // permission dialog that _acquireMeetingStream pops below burns the
        // gesture the moment it opens (see voice.primeAudio)
        voice.primeAudio();
        // a reply being read aloud would end up in the meeting recording
        try { Alpine.store("speech").stop(); } catch (e) {}

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
        this.chatId = null;
        this.segments = [];
        this.gaps = [];
        this.markers = [];
        this.wordCount = 0;
        this.paused = false;
        this.dismissed = false;
        this.finished = false;
        this.durationSec = 0;
        this.diarized = false;
        this.attendeesOpen = false;
        // a meeting in an existing room starts with that room's last attendee list
        if (targetChatId) {
            this._loadAttendees(targetChatId);
        } else {
            this.attendees = "";
        }
        this._recPart = 0;
        this.tick = 0;
        this.stripKey = 0;
        this._pausedWallMs = 0;
        this._prevDivergence = 0;
        this._startedAtWall = Date.now();
        this.title = `Meeting ${this._fmtStamp()}`;

        // with speaker labelling on, open the speech-server session the recording
        // streams to (chunks queue behind it, so the recorder needn't wait)
        this._upload = this._openSession();

        // start retentive recording immediately so the whole meeting is captured
        // to IndexedDB, including any pre-roll before the new chat finishes
        this._makeRecorder();

        if (targetChatId) {
            // in-chat meeting: reuse the open meeting room (keep its title), and
            // drop a boundary marker so this meeting's notes only see its own
            // transcript. the stream is already acquired, so a failure here must
            // tear everything back down.
            this.chatId = targetChatId;
            try {
                await simpleApiPost(`/api/chat/meeting/boundary/${this.chatId}`, { title: this.title });
            } catch (err) {
                this._abort();
                this.setError(`Failed to start the meeting in this chat: ${err.message || err}`);
                return;
            }
        } else {
            // create directly in Meetings instead of inheriting the current chat's category
            try {
                await this._createMeetingChat();
            } catch (err) {
                this._abort();
                this.setError(`Failed to create the meeting chat: ${err.message || err}`);
                return;
            }
        }

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

        this._preRollSec = (Date.now() - this._recStartMs) / 1000;
        this.active = true;
        this._tickTimer = setInterval(() => this._heartbeat(), 1000);
    },

    async _createMeetingChat() {
        const chat = Alpine.store("chat");
        // chats created by the record button are meeting rooms from the start
        await chat.newCategory(this.MEETINGS_CATEGORY, { meeting_room: true });
        this.chatId = (chat.chat && chat.chat.id) || chat.selectedChat || null;
        if (!this.chatId) {
            throw new Error("The new meeting chat could not be loaded.");
        }
        try {
            await simpleApiPost(`/api/chat/rename/${this.chatId}`, { title: this.title });
        } catch (e) {
            console.warn("meeting chat rename failed (continuing)", e);
        }
    },

    addSegment(seg) {
        if (!this.active && !this.stopping) {
            return;
        }
        this.segments.push({ tsSec: seg.tsSec, text: seg.text });
        this.wordCount = this._countWords();
        this.stripKey += 1;
    },

    addGap(g) {
        if (!this.active && !this.stopping) {
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
        this.active = false;
        this.stopping = true;
        this.finished = true;
        try {
            const voice = Alpine.store("voice");
            this._stopWatchers();
            this.durationSec = Math.round(voice.meetingElapsedSec());

            // 1. flush the recorder (final chunk lands in IDB) BEFORE the voice
            //    pipeline releases the shared stream (releaseAudio stops the tracks)
            await this._stopRecorder();

            // 2. drain the voice pipeline: waits the in-flight commit, commits the
            //    tail (emitted via onSegmentCommitted), and releases the audio
            await voice.stopMeetingRecording();

            this._releaseWakeLock();
            this._clearLock();
            if (this._stream) {
                try { this._stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
                this._stream = null;
            }
            this.durationSec = Math.round(voice.meetingElapsedSec());

            // 3. optionally replace the live transcript with a speaker-labelled one
            await this._diarize();

            // 4. send the transcript as its own user message into the meeting chat
            await this._sendTranscript();
        } catch (err) {
            this.setError(`Failed to finish the meeting: ${err.message || err}`);
            throw err;
        } finally {
            this._stopping = false;
            this.stopping = false;
        }
    },

    _diarizeEnabled() {
        try {
            const settings = Alpine.store("settings");
            const webui = settings && settings.settings && settings.settings.channels
                && settings.settings.channels.settings && settings.settings.channels.settings.webui;
            return !!(webui && webui.stt_diarize_meetings);
        } catch (err) {
            return false;
        }
    },

    // the saved recording as one blob per recorder session (a paused meeting has several)
    async _recordingParts() {
        let rec = null;
        try {
            rec = await RecordingStore.getMeeting(this.meetingId);
        } catch (e) {}
        const groups = [];
        let last;
        for (const f of (rec && rec.fragments) || []) {
            if (!groups.length || f.part !== last) {
                groups.push([]);
                last = f.part;
            }
            groups[groups.length - 1].push(f.blob);
        }
        const codec = (rec && rec.codec) || "";
        return { ext: codec.indexOf("mp4") !== -1 ? "mp4" : "webm", blobs: groups.map((g) => new Blob(g, { type: codec })) };
    },

    // replace the live transcript with one labelled by speaker. normally the speech
    // server already has the meeting (streamed while it ran) and only finishes the last
    // window; if streaming broke, the whole saved recording is uploaded instead. if
    // both fail the live transcript is kept, so the meeting is never lost
    async _diarize() {
        if (!this._diarizeEnabled() || !this.meetingId) {
            this._discardSession();
            return;
        }
        this.diarizing = true;
        try {
            let segments = null;
            try {
                segments = await this._finishSession();
            } catch (err) {
                console.warn("meeting session failed; uploading the whole recording instead", err);
            }
            // null = streaming wasn't used or failed. an empty list is a successful
            // finish that found no speech - uploading the recording again won't change that
            if (segments === null) {
                segments = await this._diarizeRecording();
            }
            if (!segments || !segments.length) {
                throw new Error("no speech was found in the recording");
            }
            // SPEAKER_n -> "Speaker 1", "Speaker 2", ... in order of first appearance
            const names = {};
            this.segments = segments.map((s) => {
                if (!(s.speaker in names)) {
                    names[s.speaker] = `Speaker ${Object.keys(names).length + 1}`;
                }
                return { tsSec: Math.max(0, s.start - this._preRollSec), text: s.text, speaker: names[s.speaker] };
            });
            this.gaps = [];             // the full recording covers live segments that failed
            this.diarized = true;
            this.wordCount = this._countWords();
        } catch (err) {
            this.setError(`Speaker labelling failed, sending the live transcript instead: ${err.message || err}`);
        } finally {
            this.diarizing = false;
        }
    },

    // waits for every chunk (including the recorder's last one) to reach the server,
    // then has it transcribe what's left. null when streaming wasn't used or broke
    async _finishSession() {
        if (this._upload) {
            await this._upload;
        }
        const id = this._sessionId;
        if (!id || this._sessionBroken) {
            this._discardSession();
            return null;
        }
        this._sessionId = null;
        this._sessionBroken = true;     // a session can only be finished once
        const raw = await (await fetch(`/api/meeting/session/${id}/finish`, { method: "POST" })).json();
        if (!raw.success) {
            throw new Error(raw.data);
        }
        return raw.data.segments;
    },

    // the fallback: send the whole saved recording in one request
    async _diarizeRecording() {
        const { ext, blobs } = await this._recordingParts();
        if (!blobs.length) {
            return null;
        }
        const form = new FormData();
        blobs.forEach((b, i) => form.append("file", b, `part${i + 1}.${ext}`));
        const raw = await (await fetch("/api/meeting/diarize", { method: "POST", body: form })).json();
        if (!raw.success) {
            throw new Error(raw.data);
        }
        return raw.data.segments;
    },

    // the notes instructions. labelled = the transcript carries voice labels (Speaker N)
    _notesInstructions(labelled, hasAttendees) {
        const speakers = labelled ? this.NOTES_SPEAKERS_LABELLED : this.NOTES_SPEAKERS_UNLABELLED;
        const listed = !hasAttendees ? [] : (labelled ? this.NOTES_ATTENDEES_LABELLED : this.NOTES_ATTENDEES_UNLABELLED);
        return ["Write meeting notes from the transcript above.", "", ...speakers, ...listed, "", ...this.NOTES_BODY]
            .join("\n");
    },

    /* ------------------------ attendees ------------------------------ */
    // the typed list as names: split on commas / semicolons / new lines, de-duplicated
    attendeeList() {
        const seen = new Set();
        const out = [];
        for (const raw of String(this.attendees || "").split(/[,;\n]/)) {
            const name = raw.trim().replace(/\s+/g, " ").slice(0, 100);
            if (name && !seen.has(name.toLowerCase())) {
                seen.add(name.toLowerCase());
                out.push(name);
            }
        }
        return out.slice(0, 50);
    },

    toggleAttendees() {
        if (this.attendeesOpen) {
            this.saveAttendees();
        } else {
            this.attendeesOpen = true;
        }
    },

    saveAttendees() {
        this.attendeesOpen = false;
        this.attendees = this.attendeeList().join(", ");
        this._persistAttendees();
    },

    // remembered on the meeting-room chat, so a recurring meeting starts pre-filled
    _persistAttendees() {
        if (!this.chatId) {
            return;
        }
        simpleApiPost(`/api/chat/meeting_attendees/${this.chatId}`, { attendees: this.attendeeList() })
            .catch((err) => console.warn("could not save the meeting attendees", err));
    },

    _loadAttendees(chatId) {
        const chat = Alpine.store("chat");
        const meta = (chat && chat.chat && chat.chat.id === chatId) ? chat.chat.metadata : null;
        const list = (meta && Array.isArray(meta.meeting_attendees)) ? meta.meeting_attendees : [];
        this.attendees = list.join(", ");
    },

    _meetingDateLabel() {
        const d = new Date(this._startedAtWall || Date.now());
        try {
            const day = d.toLocaleDateString("en-US", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
            const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
            return `${day}, ${time}`;
        } catch (e) {
            return d.toString();
        }
    },

    // the transcript lines: segments merged per speaker (up to MERGE_LINE_SEC per line),
    // with pause / interruption / gap markers in time order
    _transcriptLines() {
        const events = [];
        for (const s of this.segments) {
            events.push({ t: s.tsSec, kind: "seg", speaker: s.speaker || null, text: s.text });
        }
        for (const g of this.gaps) {
            events.push({ t: g.tsSec, kind: "mark", line: "-- segment missing (transcription failed; audio preserved locally) --" });
        }
        for (const m of this.markers) {
            if (m.kind === "pause") {
                events.push({ t: m.tsSec, kind: "mark", line: `-- recording paused for ${this._fmtClock(m.durSec)} --` });
            } else if (m.kind === "interruption") {
                events.push({ t: m.tsSec, kind: "mark", line: `-- recording interrupted ~${Math.round(m.durSec)}s (tab was backgrounded) --` });
            }
        }
        events.sort((a, b) => a.t - b.t);

        const lines = [];
        let open = null;    // the segment line being extended
        const flush = () => {
            if (open) {
                const who = open.speaker ? `${open.speaker}: ` : "";
                lines.push(`[${this._fmtClock3(open.t)}] ${who}${open.text}`);
                open = null;
            }
        };
        for (const e of events) {
            if (e.kind === "mark") {
                flush();
                lines.push(`[${this._fmtClock3(e.t)}] ${e.line}`);
            } else if (open && open.speaker === e.speaker && e.t - open.t < this.MERGE_LINE_SEC) {
                open.text = `${open.text} ${e.text}`;
            } else {
                flush();
                open = { t: e.t, speaker: e.speaker, text: e.text };
            }
        }
        flush();
        return lines;
    },

    // send the transcript, followed by the notes instructions, as one user message
    async _sendTranscript() {
        const labelled = this.diarized;
        const speakerCount = new Set(this.segments.map((s) => s.speaker).filter(Boolean)).size;
        const attendees = this.attendeeList();
        this._persistAttendees();   // typed before the meeting chat existed
        const lines = [
            `Meeting: ${this.title}`,
            `Date: ${this._meetingDateLabel()} - duration ${this._fmtClock(this.durationSec)} - ${this.wordCount} words`,
            labelled
                ? `Speakers: labelled by voice as Speaker 1 to Speaker ${speakerCount}; which label is which person is not known.`
                : "Speakers: not labelled.",
            ...(attendees.length ? [`Attendees (listed by the user): ${attendees.join(", ")}`] : []),
            "Transcribed by speech recognition: expect occasional misheard words, especially names and numbers.",
            "",
            "<transcript>",
            ...this._transcriptLines(),
            "</transcript>",
            "",
            this._notesInstructions(labelled, attendees.length > 0)
        ];
        this._notesLabelled = labelled;

        // chat_id pins the transcript to the meeting chat even if the user
        // navigated to another chat meanwhile (the server switches + broadcasts).
        // queued rather than dropped if the socket is down: after a long meeting
        // on a phone with the screen locked, it usually is
        queueSocketSend({ type: "user_message", content: lines.join("\n"), chat_id: this.chatId });
    },

    // "Regenerate notes": the same instructions as the first time, for the latest transcript
    async generateNotes() {
        if (Alpine.store("stream").state !== "idle") {
            return;
        }
        const attendees = this.attendeeList();
        const instructions = this._notesInstructions(this._notesLabelled, attendees.length > 0)
            .replace("Write meeting notes from the transcript above.",
                "Write the meeting notes again from the most recent meeting transcript in this chat.");
        // the list may have been corrected since the transcript was sent; this one wins
        const head = attendees.length
            ? `Attendees (listed by the user, replacing any earlier list): ${attendees.join(", ")}\n\n`
            : "";
        await simpleSocketSend({
            type: "user_message",
            content: head + instructions,
            chat_id: this.chatId
        });
    },

    // completion-card action: start another meeting in the same room, even if
    // the user navigated to another chat after the previous one finished
    async startNewMeeting() {
        if (!this.chatId || this.active || this.stopping) {
            return;
        }
        const chat = Alpine.store("chat");
        if (chat.selectedChat !== this.chatId) {
            await chat.loadChat(this.chatId);
        }
        if (chat.selectedChat !== this.chatId) {
            this.setError("Could not open the meeting room chat.");
            return;
        }
        await this.startMeeting({ targetChatId: this.chatId });
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
        const { ext, blobs } = await this._recordingParts();
        if (!blobs.length) {
            this.setError("No saved recording found for this meeting.");
            return;
        }
        const single = blobs.length === 1;
        for (let i = 0; i < blobs.length; i++) {
            // raw container segments don't concatenate cleanly, so multi-part
            // meetings download as N parts rather than one faked file
            const name = single
                ? `meeting-${this._fmtStampFile()}.${ext}`
                : `meeting-${this._fmtStampFile()}-part${i + 1}.${ext}`;
            this._triggerDownload(blobs[i], name);
            if (!single && i < blobs.length - 1) {
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
        this._discardSession();
        if (this.meetingId) {
            try { RecordingStore.deleteMeeting(this.meetingId); } catch (e) {}
            this.meetingId = null;
        }
        const voice = Alpine.store("voice");
        this.active = false;
        this.stopping = false;
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
