/*
 * voice input store
 *
 * captures microphone audio as raw PCM in memory (audio worklet, no files ever touch disk),
 * resampled to 16 kHz as it arrives (the browser records at its own rate, see VoiceResampler).
 *
 * dictation keeps one growing transcript:
 * - while the user talks, the whole recording so far (or its last previewMaxSec on a very
 *   long one) is transcribed about once a second and shown in a caption strip above the
 *   input bar (or not at all when voice_preview_style is "off")
 * - on stop, if the latest caption already covers everything that was said it goes
 *   straight into the input; otherwise the whole recording is transcribed once more. the
 *   input field is written exactly once per dictation and never changes afterwards
 * - after api.voice_auto_stop seconds of silence (measured on the audio clock) the
 *   dictation ends on its own; Enter (or Send) while dictating stops and sends
 *
 * meetings split the recording at pauses into VAD segments instead and hand each
 * transcribed segment to the meets store (onSegmentCommitted), so a long meeting never
 * holds more than one segment in memory.
 *
 * the client never decides how the audio is transcribed - it posts the WAV to
 * /api/voice/transcribe and the server picks the endpoint.
 */

// worklet runs on the audio thread and posts raw PCM chunks to the main thread
const VOICE_WORKLET_CODE = `
class VoiceCaptureProcessor extends AudioWorkletProcessor {
    process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (channel) {
            this.port.postMessage(channel.slice(0));
        }
        return true;
    }
}
registerProcessor("voice-capture-processor", VoiceCaptureProcessor);
`;

const VOICE_STORE = {
    recording: false,
    transcribing: false,
    error: null,

    // 'dictation' (one-shot into the input field) or 'meeting' (long-running,
    // compacts the buffer, streams committed segments to the meets store instead
    // of the composer). meeting-only behaviour is gated on this everywhere.
    mode: "dictation",

    _ctx: null,
    _stream: null,
    _source: null,
    _node: null,
    _workletUrl: null,

    _pcmBuf: null,          // 16 kHz mono (see _resampler)
    _pcmLength: 0,
    _resampler: null,       // capture rate -> targetRate
    // monotonic count of every sample appended (never reduced by compaction) -
    // the audio clock that survives buffer discards and freezes on pause
    _totalSamples: 0,
    _rate: 16000,

    _session: 0,
    _prefix: "",
    _committed: "",
    // visible in the caption strip while recording (never in the textarea)
    preview: "",
    _lastWritten: "",
    _wroteInput: false,     // this dictation put text in the input (see isDictated)

    // dictation: the latest caption that covered the recording from its start
    // ({ text, to: sample index it ran to }), so stopping can reuse it
    _heard: null,
    _previewPromise: null,
    _previewTo: -1,         // where the caption in flight runs to (-1: not from the start)
    _autoStopMs: 3000,      // from api.voice_auto_stop at start; 0 = never
    _keyHandler: null,

    // the open segment starts at sample _segStart; _lastVoiced tracks the last voiced sample
    _segStart: 0,
    _lastVoiced: -1,
    _voicedSinceSend: false,

    _tickTimer: null,
    _tickInFlight: false,
    _commitInFlight: null,
    _previewFails: 0,

    // live auto-stop tracker: measured on the audio clock and updated on EVERY
    // chunk (including while a segment commit is in flight, when _vadUpdate is
    // paused), so it can neither be delayed by a slow transcription server nor
    // fire while the user is still actually talking
    _asFloor: 0.01,        // persistent noise floor for its own voiced test
    _asLastVoiced: -1,     // sample index of the last voiced chunk
    _didVoice: false,      // has the user actually spoken in this session

    // meeting-mode only: a lower effective vadAbsMin from the sensitivity
    // slider (null = use the dictation default), plus the segment/gap callbacks
    // wired up by the meets store (both reset in dictation mode)
    meetingVadAbsMin: null,
    _gapCount: 0,
    onSegmentCommitted: null,
    onGap: null,

    // drives the "Listening… m:ss" timer in voice_preview_style "off" mode -
    // bumped by the tick interval so Alpine re-renders the label
    elapsedTick: 0,
    _startedAt: 0,

    // energy vad state: { floor: noise level, silentMs, speechMs }
    _vad: null,

    targetRate: 16000,
    // a dictation caption transcribes the whole recording so far, up to this much of its
    // end (a longer dictation shows "..." plus its last previewMaxSec)
    previewMaxSec: 90,
    previewMinSec: 1.0,      // don't caption until the recording has this much audio
    previewIntervalMs: 1000,

    // meeting segments: split after this much silence, but only once at least
    // minSpeechMs of speech has been heard, and cap a segment at maxSegmentSec
    silenceSplitMs: 1600,
    minSpeechMs: 500,
    maxSegmentSec: 20,

    // silence threshold: max(vadAbsMin, noise floor * vadFloorFactor), the floor adapts
    vadFloorFactor: 3.0,
    vadAbsMin: 0.006,

    isSupported() {
        const hasContext = !!(window.AudioContext || window.webkitAudioContext);
        return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && hasContext);
    },

    // native OS dictation (e.g. the iPhone keyboard mic) types into the focused
    // textarea transparently, so on those devices we can hint at it while keeping
    // the in-app button as the fallback (WebViews, keyboards without a mic, etc)
    nativeDictationLikely() {
        const ua = navigator.userAgent;
        const iphony = /iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
        return iphony && /Safari|CriOS|Fxios|Firefox/i.test(ua) || false;
    },

    // true on an actual iPhone/iPod, in any browser (Safari, Chrome/CriOS,
    // Firefox/Fxios, or a WKWebView/PWA). on these we hide the in-app mic button
    // and the native-dictation hint below - the user dictates with the keyboard's
    // own mic. device-only (no Safari gate, unlike nativeDictationLikely) and
    // deliberately NOT iPad and NOT width-based, so every other platform keeps its
    // current behaviour
    isIphone() {
        return /iPhone|iPod/i.test(navigator.userAgent);
    },

    // the webui channel settings are global (not per-user); the default is
    // "strip", which also covers a settings store that hasn't loaded yet
    _previewStyle() {
        try {
            const settings = Alpine.store("settings");
            const webui = settings && settings.settings && settings.settings.channels
                && settings.settings.channels.settings && settings.settings.channels.settings.webui;
            return (webui && webui.voice_preview_style) || "strip";
        } catch (err) {
            return "strip";
        }
    },

    // per-user api.voice_auto_stop (seconds of silence, 0 = off); default 3 s. shorter
    // cuts people off while they pause to think
    _readAutoStopMs() {
        try {
            const api = Alpine.store("settings").settings.api;
            const sec = Number(api && api.voice_auto_stop);
            if (api && api.voice_auto_stop !== undefined && api.voice_auto_stop !== null
                && api.voice_auto_stop !== "" && Number.isFinite(sec)) {
                return Math.max(0, sec) * 1000;
            }
        } catch (err) { /* settings not loaded yet */ }
        return 3000;
    },

    // "m:ss" elapsed recording time for the "off" mode label. reads elapsedTick
    // (even though it doesn't need it) so Alpine re-renders the label on every tick
    elapsedLabel() {
        void this.elapsedTick;
        if (!this.recording || !this._startedAt) {
            return "0:00";
        }
        const totalSec = Math.floor((Date.now() - this._startedAt) / 1000);
        const m = Math.floor(totalSec / 60);
        const s = totalSec % 60;
        return `${m}:${String(s).padStart(2, "0")}`;
    },

    // creates (or resumes) the AudioContext. the iOS twist: Safari on iPhone /
    // iPad only starts audio from a context created - or resumed - during a
    // LIVE user gesture (transient activation); desktop browsers use sticky
    // activation, where any gesture the page ever received suffices. a single
    // await (the getUserMedia permission dialog, a websocket round-trip, ...)
    // burns the gesture, so every entry point that can still hold one (the
    // meeting tap, the dictation tap) calls this in its own synchronous prefix,
    // BEFORE its first await. startRecording reuses the primed context, so this
    // never creates a second one
    primeAudio() {
        try {
            if (!this._ctx) {
                // at the device's own rate: Firefox refuses to connect a microphone to a
                // context running at a different rate, so we resample ourselves
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

    async startRecording(opts = {}) {
        if (this.recording) {
            return true;
        }
        this.error = null;
        if (!this.isSupported()) {
            this.error = "Voice input is not supported in this browser.";
            return false;
        }

        // prime the context now: for dictation this call's own synchronous
        // prefix still runs inside the tap that started it, so the gesture is
        // alive here even though the getUserMedia dialog below will burn it
        this.primeAudio();

        // a reply being read aloud would be picked up by the mic
        try { Alpine.store("speech").stop(); } catch (e) {}

        const mode = (opts && opts.mode === "meeting") ? "meeting" : "dictation";
        this.mode = mode;
        // in dictation mode clear everything the meetings store may have set, so a
        // stale callback can never fire on a plain dictation commit
        if (mode !== "meeting") {
            this.meetingVadAbsMin = null;
            this.onSegmentCommitted = null;
            this.onGap = null;
        }

        const chat = Alpine.store("chat");
        this._prefix = chat ? (chat.user_input || "").trim() : "";
        this._lastWritten = chat ? (chat.user_input || "") : "";
        this._wroteInput = false;
        this._committed = "";
        this.preview = "";
        this._heard = null;
        this._previewPromise = null;
        this._previewTo = -1;
        this._autoStopMs = this._readAutoStopMs();
        this.elapsedTick = 0;
        this._startedAt = Date.now();
        this._totalSamples = 0;
        this._gapCount = 0;
        this._segStart = 0;
        this._lastVoiced = -1;
        this._voicedSinceSend = false;
        this._previewFails = 0;
        this._commitInFlight = null;
        this._asFloor = 0.01;
        this._asLastVoiced = -1;
        this._didVoice = false;

        // the meets store may already own the MediaStream (it also feeds a
        // MediaRecorder from it to retain the audio on disk) - when a stream is
        // supplied we skip getUserMedia entirely and just share it
        let stream = (opts && opts.stream) || null;
        if (!stream) {
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    audio: { echoCancellation: true, noiseSuppression: true }
                });
            } catch (err) {
                this.error = err.name === "NotAllowedError"
                    ? "Microphone access was denied. Please allow microphone access in your browser settings."
                    : `Failed to start recording: ${err.message}`;
                return false;
            }
        }
        this._stream = stream;

        try {
            // reuse the context primed inside the tap (see primeAudio()); only
            // create one here if that prime couldn't run, so the failure still
            // lands in this guarded block
            if (!this._ctx) {
                const Ctx = window.AudioContext || window.webkitAudioContext;
                this._ctx = new Ctx();
            }
            // everything past _appendPcm works on 16 kHz audio
            this._rate = this.targetRate;
            this._resampler = new VoiceResampler(this._ctx.sampleRate, this.targetRate);

            this._workletUrl = URL.createObjectURL(new Blob([VOICE_WORKLET_CODE], { type: "application/javascript" }));
            await this._ctx.audioWorklet.addModule(this._workletUrl);

            // a context that is not running means no audio will ever flow (on
            // iOS: the gesture was gone before the context got primed). fail
            // loudly instead of sitting on a silent "recording" that never
            // transcribes
            if (this._ctx.state !== "running") {
                try { await this._ctx.resume(); } catch (err) { /* stays suspended */ }
            }
            if (this._ctx.state !== "running") {
                this._releaseAudio();
                this.error = "The browser blocked the audio context. Allow microphone access, then tap the button again.";
                return false;
            }

            this._pcmBuf = new Float32Array(this._rate * 30);
            this._pcmLength = 0;
            this._vad = this._newVad();

            this._source = this._ctx.createMediaStreamSource(this._stream);
            this._node = new AudioWorkletNode(this._ctx, "voice-capture-processor");
            this._node.port.onmessage = (e) => this._appendPcm(e.data);
            this._source.connect(this._node);

            this._session++;
            this._tickInFlight = false;
            this.recording = true;
            if (mode !== "meeting") {
                this._listenForEnter();
            }

            this._tickTimer = setInterval(
                () => this._tickPreview(this._session),
                this.previewIntervalMs
            );
            return true;
        } catch (err) {
            this._releaseAudio();
            this.error = `Failed to start recording: ${err.message}`;
            return false;
        }
    },

    _newVad() {
        return { floor: 0.01, silenceMs: 0, speechMs: 0 };
    },

    // effective silence floor: meeting mode can lower it via the sensitivity
    // slider to hear quiet / distant speakers; dictation always uses vadAbsMin
    _effectiveVadAbsMin() {
        return (this.mode === "meeting" && this.meetingVadAbsMin != null)
            ? this.meetingVadAbsMin
            : this.vadAbsMin;
    },

    // audio-clock elapsed time in seconds: total captured samples / rate. unlike
    // _pcmLength it is never reduced by compaction, and it freezes while the
    // context is suspended (pause), so it doubles as the meeting timer base and
    // the source for per-segment timestamps
    meetingElapsedSec() {
        return this._totalSamples / this._rate;
    },

    // commit-and-discard: drop the buffer region [0, toSample) that has just been
    // committed (its PCM is already copied into a separate `pcm` array), rebase the
    // absolute sample indices that _vadUpdate/_appendPcm still read down by the
    // discarded amount, and reset the open segment to the start of the surviving
    // tail. the RELATIVE positions the VAD reads (pcmLength - segStart, pcmLength
    // - lastVoiced) are preserved exactly, so split points are unchanged. meeting
    // mode only - dictation keeps its one growing buffer (a dictation ends fast).
    _compactTo(toSample) {
        if (toSample >= this._pcmLength) {
            this._pcmBuf = new Float32Array(0);
            this._pcmLength = 0;
        } else {
            const keep = this._pcmBuf.slice(toSample, this._pcmLength);
            this._pcmBuf = keep;
            this._pcmLength = keep.length;
        }
        if (this._asLastVoiced >= toSample) this._asLastVoiced -= toSample; else this._asLastVoiced = -1;
        if (this._lastVoiced >= toSample) this._lastVoiced -= toSample; else this._lastVoiced = -1;
        this._segStart = 0;
    },

    _appendPcm(raw) {
        if (!this._pcmBuf || !this._resampler) {
            return;
        }
        const chunk = this._resampler.process(raw);
        if (!chunk.length) {
            return;
        }
        if (this._pcmBuf.length - this._pcmLength < chunk.length) {
            const bigger = new Float32Array(Math.max(this._pcmBuf.length * 2, this._pcmLength + chunk.length));
            bigger.set(this._pcmBuf);
            this._pcmBuf = bigger;
        }
        this._pcmBuf.set(chunk, this._pcmLength);
        this._pcmLength += chunk.length;
        this._totalSamples += chunk.length;   // monotonic audio clock (survives compaction)

        // track the chunk's energy for silence detection (segment splitting)
        let sum = 0;
        for (let i = 0; i < chunk.length; i++) {
            const s = chunk[i];
            sum += s * s;
        }
        const rms = Math.sqrt(sum / chunk.length);
        this._vadUpdate(rms, (chunk.length / this._rate) * 1000);

        // live auto stop: its own always-running tracker (see _asFloor above), so
        // the audio clock - not the transcribe spinner - decides when the session
        // ends: api.voice_auto_stop seconds after the last word, even if the previous
        // segment is still being transcribed. it tracks real voice activity, so it
        // never stops the user while they are still talking, and _didVoice keeps a
        // fresh idle recording running until they actually speak. meetings never
        // auto-stop (explicit stop only), so the whole block is gated off there.
        this._asFloor = rms < this._asFloor ? rms : Math.min(0.02, this._asFloor * 1.002);
        const asThreshold = Math.max(this._effectiveVadAbsMin(), this._asFloor * this.vadFloorFactor);
        if (rms >= asThreshold) {
            this._asLastVoiced = this._pcmLength;
            this._didVoice = true;
            this._voicedSinceSend = true;   // something new for the next caption
        }
        if (
            this.mode !== "meeting" &&
            this.recording && this._didVoice && this._autoStopMs > 0 &&
            this._asLastVoiced !== -1 &&
            this._pcmLength - this._asLastVoiced >= (this._autoStopMs / 1000) * this._rate
        ) {
            this.stopRecording();
        }
    },

    // lightweight energy vad: splits the open segment once the user has been silent
    // long enough (after having actually spoken), or once the segment gets too long
    _vadUpdate(rms, ms) {
        // meetings only: a dictation is transcribed whole, never in segments
        if (this.mode !== "meeting" || !this.recording || !this._vad || this._commitInFlight) {
            return;
        }

        const vad = this._vad;
        const segmentMs = ((this._pcmLength - this._segStart) / this._rate) * 1000;

        // low-envelope noise floor: snaps down to new quiet levels, drifts up very slowly
        vad.floor = rms < vad.floor ? rms : Math.min(0.02, vad.floor * 1.002);
        const threshold = Math.max(this._effectiveVadAbsMin(), vad.floor * this.vadFloorFactor);

        if (rms >= threshold) {
            // voiced audio
            this._lastVoiced = this._pcmLength;
            this._voicedSinceSend = true;
            vad.speechMs += ms;
            vad.silenceMs = 0;

            // time-based split: cut at the last voiced sample (word boundary-ish),
            // leaving a tiny margin so the next segment doesn't start mid-word
            if (vad.speechMs >= this.minSpeechMs && segmentMs >= this.maxSegmentSec * 1000) {
                this._splitSegment(this._lastVoiced + Math.floor(this._rate * 0.03));
            }
            return;
        }

        if (vad.speechMs < this.minSpeechMs) {
            // no speech heard in this segment yet, don't auto-split a fresh recording
            return;
        }

        vad.silenceMs += ms;
        if (vad.silenceMs >= this.silenceSplitMs) {
            // the pause itself is uninformative for transcription, so the next
            // segment starts right where this chunk ends
            this._splitSegment(this._pcmLength);
        }
    },

    // commits the current open segment, then opens a fresh one at newSegStart
    _splitSegment(newSegStart) {
        const session = this._session;
        const segStart = this._segStart;
        this._vad = null; // pause splitting while the commit is in flight

        this._commitInFlight = this._commitSegment(segStart, newSegStart).finally(() => {
            if (session === this._session) {
                // compaction already reset _segStart to 0 in meeting mode; don't
                // write back the stale absolute value captured before the discard
                this._segStart = this.mode === "meeting" ? 0 : newSegStart;
                if (this.recording) {
                    this._vad = this._newVad();
                }
            }
            this._commitInFlight = null;
        });
    },

    // transcribes one finished segment and appends the result to the committed text
    // buffer. the input field is NOT written here - a mid-session commit happens on
    // every normal pause while the user is still speaking, and the field only gets
    // the text once the session ends (stopRecording)
    async _commitSegment(fromSample, toSample) {
        const session = this._session;
        const rate = this._rate;

        if (toSample - fromSample < rate * 0.4) {
            // too short to be worth transcribing
            return;
        }

        // absolute timeline offset of this segment's first sample, computed from
        // the monotonic clock BEFORE compaction shifts the buffer. (total - bufLen)
        // is exactly how many samples have been discarded so far, so the sum lands
        // on the segment's real position on the (virtual) timeline.
        const tsSec = ((this._totalSamples - this._pcmLength) + fromSample) / rate;

        const pcm = this._pcmBuf.slice(fromSample, toSample); // copy the source region out first
        // meeting mode: the committed region is now safely copyable away, so drop
        // it and keep the buffer O(one segment) instead of O(the whole meeting)
        if (this.mode === "meeting") {
            this._compactTo(toSample);
        }
        this.transcribing = true;

        // encode once - the retry below re-POSTs this exact wav
        const wav = pcmToWavBase64(pcm, rate, this.targetRate);
        const doPost = () => simpleApiPost("/api/voice/transcribe", {
            audio_data: wav,
            format: "wav",
            purpose: "commit"
        });
        const acceptText = (text) => {
            if (text) {
                this._committed = this._committed ? `${this._committed} ${text}`.trim() : text;
                this._previewFails = 0;
            }
            // meetings stream each committed segment to the meets store (dictation
            // has no callback). a success with empty text is silence, not a gap.
            if (this.mode === "meeting" && text && this.onSegmentCommitted) {
                this.onSegmentCommitted({ tsSec, text });
            }
        };

        try {
            const result = await doPost();
            const text = (result && result.text) ? String(result.text).trim() : "";
            // only a brand-new recording invalidates this commit
            if (session !== this._session) {
                return;
            }
            acceptText(text);
        } catch (err) {
            if (session !== this._session) {
                return;
            }
            if (this.mode === "meeting") {
                // a meeting must survive a failed commit: retry once, then leave a
                // visible gap marker (the audio is already in IndexedDB). never set
                // this.error - a hiccup must not tear down the whole session.
                try {
                    const retry = await doPost();
                    const text = (retry && retry.text) ? String(retry.text).trim() : "";
                    if (session === this._session) {
                        acceptText(text);
                    }
                } catch (err2) {
                    this.onGap && this.onGap({ tsSec, index: (this._gapCount = (this._gapCount || 0) + 1) });
                }
            } else {
                this.error = `Transcription failed: ${err}`;
            }
        } finally {
            if (session === this._session) {
                this.transcribing = false;
            }
        }
    },

    // dictation caption: transcribes the whole recording so far (or its last
    // previewMaxSec) and shows it in the caption strip. one request at a time
    _tickPreview(session) {
        if (!this.recording || session !== this._session) {
            return;
        }
        if (this.mode === "meeting") {
            // meetings never send preview requests at all (commits only); the
            // caption strip shows the last committed segment instead, driven by
            // the meets store. skip the whole preview machinery here.
            return;
        }
        if (this._previewStyle() === "off") {
            // no live caption: skip the request and just drive the "Listening… m:ss" timer
            this.elapsedTick += 1;
            return;
        }
        const rate = this._rate;
        const end = this._pcmLength;
        if (this._tickInFlight || end < this.previewMinSec * rate) {
            return;
        }
        // send when something new was said; or once more after the last word when the
        // latest caption stopped short of its end, so stopping can reuse the caption
        // instead of transcribing again. pure silence never reaches the server
        const covers = this._heard && this._heard.to >= this._spokeUntil();
        const catchUp = this._didVoice && !covers && end >= this._spokeUntil()
            && end <= this.previewMaxSec * rate;
        if (!this._voicedSinceSend && !catchUp) {
            return;
        }
        const from = Math.max(0, end - Math.floor(this.previewMaxSec * rate));
        this._voicedSinceSend = false;

        let wav;
        try {
            wav = pcmToWavBase64(this._pcmBuf.subarray(from, end), rate, this.targetRate);
        } catch (err) {
            return;
        }

        this._tickInFlight = true;
        this._previewTo = from === 0 ? end : -1;
        this._previewPromise = simpleApiPost("/api/voice/transcribe", { audio_data: wav, format: "wav" })
            .then((result) => {
                if (session !== this._session) {
                    return;
                }
                const text = (result && result.text) ? String(result.text).trim() : "";
                this._previewFails = 0;
                if (from === 0) {
                    this._heard = { text, to: end };
                }
                if (text && this.recording) {
                    this.preview = from > 0 ? `… ${text}` : text;
                }
            })
            .catch(() => {
                if (session !== this._session || !this.recording) {
                    return;
                }
                this._previewFails++;
                if (this._previewFails === 3) {
                    this.error = "Voice transcription keeps failing (STT server unreachable?).";
                }
            })
            .finally(() => {
                this._tickInFlight = false;
            });
    },

    _fullText() {
        return this._committed;
    },

    // writes the dictated text into the chat input after any text that was already
    // there. only called once the dictation has ended - the field never changes while
    // the user is still speaking. skips the write if the user edited the field meanwhile
    _writeInput() {
        if (this.recording) {
            return;
        }
        const chat = Alpine.store("chat");
        if (!chat) {
            return;
        }
        if (chat.user_input !== this._lastWritten) {
            return;
        }
        const text = this._committed;
        const next = this._prefix ? `${this._prefix} ${text}`.trim() : text;
        chat.user_input = next;
        this._lastWritten = next;
        this._wroteInput = this.mode !== "meeting" && !!text.trim();
    },

    // true when `text` (being sent) is exactly what the last dictation wrote into the
    // input - i.e. the message was spoken, not typed or edited afterwards. called once
    // per send: it uses the flag up, so a later typed message never counts as spoken
    isDictated(text) {
        const spoken = this._wroteInput && String(text || "").trim() === String(this._lastWritten || "").trim();
        this._wroteInput = false;
        return spoken;
    },

    // where the last thing said ends: a word's quiet tail can sit under the voice
    // threshold, so a caption must run a little past the last voiced sample to cover it
    _spokeUntil() {
        return this._asLastVoiced + Math.floor(this._rate * 0.3);
    },

    // ends a dictation: puts the transcript in the input once. the latest caption is
    // used as-is when it already covers the last thing said; otherwise the whole
    // recording is transcribed in one request
    async _finishDictation() {
        if (!this.recording) {
            return this._fullText();
        }
        const session = this._session;
        this.recording = false;
        this._startedAt = 0;
        this._vad = null;
        if (this._tickTimer) {
            clearInterval(this._tickTimer);
            this._tickTimer = null;
        }

        const rate = this._rate;
        const pcm = this._pcmBuf ? this._pcmBuf.slice(0, this._pcmLength) : new Float32Array(0);
        const spoke = this._didVoice;
        const spokeUntil = Math.min(pcm.length, this._spokeUntil());
        this._releaseAudio();
        this._pcmBuf = null;
        this._pcmLength = 0;

        if (!spoke || pcm.length < rate * 0.4) {
            this.preview = "";
            return this._fullText();
        }

        this.transcribing = true;
        try {
            // a caption still in flight is only worth waiting for if it will cover the
            // end; otherwise transcribing the whole recording now is quicker
            if (this._tickInFlight && this._previewPromise && this._previewTo >= spokeUntil) {
                await this._previewPromise.catch(() => {});
            }
            if (session !== this._session) {
                return this._fullText();
            }
            const heard = this._heard;
            let text;
            if (heard && heard.text && heard.to >= spokeUntil) {
                text = heard.text;
            } else {
                try {
                    const result = await simpleApiPost("/api/voice/transcribe", {
                        audio_data: pcmToWavBase64(pcm, rate, this.targetRate),
                        format: "wav"
                    });
                    text = (result && result.text) ? String(result.text).trim() : "";
                } catch (err) {
                    // keep what the caption heard rather than losing the dictation
                    text = (heard && heard.text) || "";
                    this.error = `Transcription failed: ${err}`;
                }
            }
            if (session === this._session) {
                this._committed = text;
                this.preview = "";
                this._writeInput();
            }
        } finally {
            if (session === this._session) {
                this.transcribing = false;
            }
        }
        return this._fullText();
    },

    // Enter (without Shift) while dictating stops and sends, wherever focus is. capture
    // phase on window, so the textarea's own Enter handler doesn't also send
    _listenForEnter() {
        this._stopListeningForEnter();
        this._keyHandler = (e) => {
            if (e.key !== "Enter" || e.shiftKey || e.isComposing || e.keyCode === 229) {
                return;
            }
            if (!this.recording || this.mode === "meeting") {
                return;
            }
            // on phones Enter is a new line, as in the input field itself
            try { if (Alpine.store("ui").isMobile) { return; } } catch (err) { /* no ui store */ }
            e.preventDefault();
            e.stopPropagation();
            this.stopAndSend();
        };
        window.addEventListener("keydown", this._keyHandler, true);
    },

    _stopListeningForEnter() {
        if (this._keyHandler) {
            window.removeEventListener("keydown", this._keyHandler, true);
            this._keyHandler = null;
        }
    },

    // stops the dictation and sends whatever ends up in the input
    async stopAndSend() {
        if (!this.recording || this.mode === "meeting") {
            return;
        }
        await this.stopRecording();
        const chat = Alpine.store("chat");
        const text = (chat && chat.user_input) || "";
        if (!text.trim() && !Alpine.store("upload").files.length) {
            return;
        }
        await chat.send(text);
        const input = document.getElementById("user-input");
        if (input) {
            input.style.height = "48px";
        }
    },

    // meetings: stops capture, waits for any in-flight segment commit, commits the
    // remaining open segment (the tail) and releases the audio. the meets store owns
    // the transcript, so the input field is never written
    async _finishCapture() {
        if (!this.recording) {
            return this._fullText();
        }
        const session = this._session;
        this.recording = false;
        this._startedAt = 0;
        this._vad = null;

        if (this._tickTimer) {
            clearInterval(this._tickTimer);
            this._tickTimer = null;
        }

        // let any in-flight segment commit land first - it commits the earlier part
        // of the recording, and the tail below commits what comes after it
        if (this._commitInFlight) {
            await this._commitInFlight.catch(() => {});
            this._commitInFlight = null;
        }

        const rate = this._rate;
        const tailTsSec = ((this._totalSamples - this._pcmLength) + this._segStart) / rate;
        const pcm = this._pcmBuf ? this._pcmBuf.slice(this._segStart, this._pcmLength) : new Float32Array(0);

        this._releaseAudio();
        this._pcmBuf = null;
        this._pcmLength = 0;

        if (pcm.length < rate * 0.4) {
            // nothing meaningful left in the open segment
            return this._fullText();
        }

        this.transcribing = true;
        try {
            const wav = pcmToWavBase64(pcm, rate, this.targetRate);
            const result = await simpleApiPost("/api/voice/transcribe", {
                audio_data: wav,
                format: "wav",
                purpose: "commit"
            });
            const text = (result && result.text) ? String(result.text).trim() : "";
            if (text) {
                this._committed = this._committed ? `${this._committed} ${text}`.trim() : text;
                this._previewFails = 0;
                if (this.onSegmentCommitted) {
                    this.onSegmentCommitted({ tsSec: tailTsSec, text });
                }
            }
        } catch (err) {
            // the tail is the user's last words - never drop it silently. a
            // failed tail commit becomes a gap marker instead of an error.
            this.onGap && this.onGap({ tsSec: tailTsSec, index: (this._gapCount = (this._gapCount || 0) + 1) });
        } finally {
            this.transcribing = false;
        }
        return this._fullText();
    },

    // stops dictation capture and writes the transcript into the input field
    async stopRecording() {
        return this._finishDictation();
    },

    // stops meeting capture and commits the tail but NEVER writes the composer -
    // the meets store owns the transcript and renders it as its own message
    async stopMeetingRecording() {
        return this._finishCapture();
    },

    // pause/resume the live capture. suspending the AudioContext freezes the
    // worklet, so _totalSamples stops advancing on its own - the pause is
    // excluded from the meeting audio clock for free
    async pauseCapture() {
        return this._ctx ? this._ctx.suspend() : null;
    },

    async resumeCapture() {
        return this._ctx ? this._ctx.resume() : null;
    },

    _releaseAudio() {
        this._stopListeningForEnter();
        this._resampler = null;
        try { if (this._node) { this._node.disconnect(); } } catch (err) { /* already disconnected */ }
        try { if (this._source) { this._source.disconnect(); } } catch (err) { /* already disconnected */ }
        if (this._stream) {
            this._stream.getTracks().forEach(t => t.stop());
            this._stream = null;
        }
        if (this._ctx && this._ctx.state !== "closed") {
            this._ctx.close();
        }
        this._node = null;
        this._source = null;
        this._ctx = null;
        if (this._workletUrl) {
            URL.revokeObjectURL(this._workletUrl);
            this._workletUrl = null;
        }
    }
};

/*
 * streaming resampler for the microphone: low-pass filters at the capture rate (a
 * windowed-sinc FIR just under the target's Nyquist frequency, so 48 kHz audio
 * doesn't alias into the speech band when thinned out) and then interpolates linearly
 * at the target rate. chunks can be any size; state carries across them
 */
class VoiceResampler {
    constructor(fromRate, toRate) {
        this.ratio = fromRate / toRate;
        this.passthrough = fromRate === toRate;
        if (this.passthrough) {
            return;
        }
        if (fromRate > toRate) {
            const taps = 63;
            const mid = (taps - 1) / 2;
            const fc = (0.45 * toRate) / fromRate;      // cutoff, cycles per input sample
            const h = new Float32Array(taps);
            let sum = 0;
            for (let i = 0; i < taps; i++) {
                const x = i - mid;
                const sinc = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
                const w = 0.42 - 0.5 * Math.cos((2 * Math.PI * i) / (taps - 1))
                    + 0.08 * Math.cos((4 * Math.PI * i) / (taps - 1));   // Blackman
                h[i] = sinc * w;
                sum += h[i];
            }
            for (let i = 0; i < taps; i++) {
                h[i] /= sum;
            }
            this.taps = h;
        } else {
            this.taps = new Float32Array([1]);           // upsampling: interpolation only
        }
        this.hist = new Float32Array(this.taps.length - 1);  // last inputs of the previous chunk
        this.last = 0;     // last filtered sample of the previous chunk
        this.base = 0;     // stream index of this chunk's first sample
        this.t = 0;        // stream position of the next output sample
    }

    process(input) {
        if (this.passthrough) {
            return input;
        }
        const h = this.taps;
        const n = h.length;
        const len = input.length;
        const ext = new Float32Array(n - 1 + len);
        ext.set(this.hist);
        ext.set(input, n - 1);
        const f = new Float32Array(len);
        for (let i = 0; i < len; i++) {
            let acc = 0;
            for (let j = 0; j < n; j++) {
                acc += h[j] * ext[i + n - 1 - j];
            }
            f[i] = acc;
        }
        this.hist = ext.slice(len);

        const out = [];
        const lastIndex = this.base + len - 1;
        while (Math.floor(this.t) + 1 <= lastIndex) {
            const i0 = Math.floor(this.t) - this.base;   // -1 means the previous chunk's last sample
            const frac = this.t - Math.floor(this.t);
            const a = i0 < 0 ? this.last : f[i0];
            out.push(a + (f[i0 + 1] - a) * frac);
            this.t += this.ratio;
        }
        if (len) {
            this.last = f[len - 1];
        }
        this.base += len;
        return Float32Array.from(out);
    }
}

/* resamples float32 PCM linearly from one sample rate to another */
function linearResample(input, fromRate, toRate) {
    if (fromRate === toRate) {
        return input;
    }
    const outLength = Math.floor(input.length * toRate / fromRate);
    const output = new Float32Array(outLength);
    const ratio = fromRate / toRate;
    for (let i = 0; i < outLength; i++) {
        const pos = i * ratio;
        const i0 = Math.floor(pos);
        const i1 = Math.min(i0 + 1, input.length - 1);
        const frac = pos - i0;
        output[i] = input[i0] * (1 - frac) + input[i1] * frac;
    }
    return output;
}

/* encodes float32 mono PCM as a 16-bit WAV and returns it base64-encoded (all in memory) */
function pcmToWavBase64(samples, sampleRate, targetRate) {
    const pcm = linearResample(samples, sampleRate, targetRate);
    const numFrames = pcm.length;
    const dataSize = numFrames * 2;

    const arrayBuffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(arrayBuffer);

    const writeString = (offset, str) => {
        for (let i = 0; i < str.length; i++) {
            view.setUint8(offset + i, str.charCodeAt(i));
        }
    };

    writeString(0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);           // fmt chunk size
    view.setUint16(20, 1, true);            // audio format: PCM
    view.setUint16(22, 1, true);            // mono
    view.setUint32(24, targetRate, true);
    view.setUint32(28, targetRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);           // bits per sample
    writeString(36, "data");
    view.setUint32(40, dataSize, true);

    let offset = 44;
    for (let i = 0; i < numFrames; i++) {
        const sample = Math.max(-1, Math.min(1, pcm[i] || 0));
        view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7FFF, true);
        offset += 2;
    }

    return arrayBufferToBase64(arrayBuffer);
}

function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
    }
    return btoa(binary);
}
