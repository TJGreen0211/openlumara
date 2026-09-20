/*
 * voice input store
 *
 * captures microphone audio as raw PCM in memory (audio worklet, no files ever touch disk)
 * and transcribes it in VAD-split segments:
 *
 * - while the user is talking, the last few seconds of the open segment are sent as a
 *   live preview (purpose "preview") and shown in a caption strip above the input bar
 *   (or not at all when voice_preview_style is "off")
 * - when the user pauses (or a segment gets long enough), the finished segment is
 *   transcribed (purpose "commit") and appended to the committed text buffer. the
 *   input field itself is only written once the session ends (manual or automatic
 *   stop), never while the user is still speaking
 * - on stop, the remaining open segment is committed the same way
 * - if the user stops speaking and stays silent for autoStopSilenceMs (measured on
 *   the audio clock, even while a segment commit is still in flight), the session
 *   ends on its own (no need to click the mic again)
 *
 * committed segments are disjoint slices of the recording, so nothing is ever
 * re-transcribed or duplicated. the client never decides how the audio is transcribed -
 * it just posts the WAV to /api/voice/transcribe and the server picks the engine.
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

    _pcmBuf: null,
    _pcmLength: 0,
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

    // increments every time the displayed preview text is accepted, so the
    // template can alternate two fade-in animations and restart them (a plain
    // class toggle would not re-trigger a CSS animation)
    previewKey: 0,

    // drives the "Listening… m:ss" timer in voice_preview_style "off" mode -
    // bumped by the tick interval so Alpine re-renders the label
    elapsedTick: 0,
    _startedAt: 0,

    // energy vad state: { floor: noise level, silentMs, speechMs }
    _vad: null,

    targetRate: 16000,
    previewWindowSec: 3,     // how much of the open segment the live preview covers
    previewMinSec: 1.2,      // don't show a preview until the segment has this much audio
    previewIntervalMs: 1500,

    // segment splitting: split after this much silence, but only once at least
    // minSpeechMs of speech has been heard. keep the silence long enough that a
    // thinking pause doesn't cut the capture off.
    silenceSplitMs: 1600,
    minSpeechMs: 500,
    // time-based split: cap open segments so a long continuous dictation doesn't
    // build one enormous segment (bound on commit latency too)
    maxSegmentSec: 20,
    // end the whole session once the user has been speaking and then stays silent
    // this long. deliberately much longer than silenceSplitMs so a thinking pause
    // between sentences never ends the capture - it only commits a segment.
    // end the whole session once the user has been speaking and then stays silent
    // this long. measured on the audio clock from the last voiced sample (see
    // _appendPcm), so the countdown keeps running while the previous segment's
    // commit is still in flight - a slow transcription server can't delay the stop.
    // deliberately much longer than silenceSplitMs so a thinking pause between
    // sentences never ends the capture - it only commits a segment.
    autoStopSilenceMs: 3000,

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

    // word-level dice similarity (0..1) between the currently displayed preview
    // and a fresh transcription of the sliding window. when whisper re-decodes
    // nearly the same audio it produces nearly the same words (~1.0); when the
    // tail has genuinely moved on, little overlaps. skipping the near-identical
    // swaps is what stops the strip flickering.
    _previewSimilarity(a, b) {
        const words = (s) => s.toLowerCase().split(/\s+/).filter(Boolean);
        const aw = words(a);
        const bw = words(b);
        if (!aw.length || !bw.length) {
            return 0;
        }
        const setA = new Set(aw);
        const setB = new Set(bw);
        let common = 0;
        setA.forEach((w) => {
            if (setB.has(w)) {
                common++;
            }
        });
        return (2 * common) / (aw.length + bw.length);
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
        this._committed = "";
        this.preview = "";
        this.previewKey = 0;
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
            const Ctx = window.AudioContext || window.webkitAudioContext;
            this._ctx = new Ctx({ sampleRate: this.targetRate });
            this._rate = this._ctx.sampleRate;

            this._workletUrl = URL.createObjectURL(new Blob([VOICE_WORKLET_CODE], { type: "application/javascript" }));
            await this._ctx.audioWorklet.addModule(this._workletUrl);

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

    _appendPcm(chunk) {
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
        // ends: autoStopSilenceMs after the last word, even if the previous
        // segment is still being transcribed. it tracks real voice activity, so it
        // never stops the user while they are still talking, and _didVoice keeps a
        // fresh idle recording running until they actually speak. meetings never
        // auto-stop (explicit stop only), so the whole block is gated off there.
        this._asFloor = rms < this._asFloor ? rms : Math.min(0.02, this._asFloor * 1.002);
        const asThreshold = Math.max(this._effectiveVadAbsMin(), this._asFloor * this.vadFloorFactor);
        if (rms >= asThreshold) {
            this._asLastVoiced = this._pcmLength;
            this._didVoice = true;
        }
        if (
            this.mode !== "meeting" &&
            this.recording && this._didVoice &&
            this._asLastVoiced !== -1 &&
            this._pcmLength - this._asLastVoiced >= (this.autoStopSilenceMs / 1000) * this._rate
        ) {
            this.stopRecording();
        }
    },

    // lightweight energy vad: splits the open segment once the user has been silent
    // long enough (after having actually spoken), or once the segment gets too long
    _vadUpdate(rms, ms) {
        if (!this.recording || !this._vad || this._commitInFlight) {
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

    // sends the most recent window of the open segment as a live preview and replaces the tail
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
            // no live preview at all: skip the request entirely and just drive
            // the "Listening… m:ss" timer
            this.elapsedTick += 1;
            return;
        }
        if (this._tickInFlight || this._commitInFlight) {
            return;
        }
        if (!this._voicedSinceSend) {
            // nothing new spoken since the last send - skip it. this also keeps
            // silence windows from reaching whisper, which hallucinates on those
            return;
        }

        const rate = this._rate;
        const segmentSec = (this._pcmLength - this._segStart) / rate;
        if (segmentSec < this.previewMinSec) {
            return;
        }

        // take the last window of the open segment (capped at everything in it so far)
        const windowSamples = Math.floor(Math.min(this.previewWindowSec, segmentSec) * rate);
        const pcm = this._pcmBuf.slice(this._pcmLength - windowSamples, this._pcmLength);
        this._voicedSinceSend = false;

        let wav;
        try {
            wav = pcmToWavBase64(pcm, rate, this.targetRate);
        } catch (err) {
            return;
        }

        this._tickInFlight = true;
        simpleApiPost("/api/voice/transcribe", {
            audio_data: wav,
            format: "wav",
            purpose: "preview"
        })
            .then((result) => {
                if (session !== this._session || !this.recording) {
                    return;
                }
                const text = (result && result.text) ? String(result.text).trim() : "";
                this._previewFails = 0;
                // stability: only swap what's on screen when the tail has genuinely
                // moved on. near-identical re-decodes of the sliding window are
                // skipped (that's the flicker), and an accepted update fades in
                // (previewKey drives the alternating animation). the preview never
                // touches the textarea.
                if (text && text !== this.preview && this._previewSimilarity(this.preview, text) < 0.7) {
                    this.preview = text;
                    this.previewKey += 1;
                }
            })
            .catch(() => {
                if (session !== this._session || !this.recording) {
                    return;
                }
                this._previewFails++;
                if (this._previewFails === 3) {
                    this.error = "Voice transcription keeps failing (STT server unreachable?). The last good text is kept.";
                }
            })
            .finally(() => {
                this._tickInFlight = false;
            });
    },

    _fullText() {
        return this._committed;
    },

    // writes the *committed* text into the chat input, preserving the
    // pre-recording text. only ever called once the session has ended - while the
    // user is still speaking the field must not change at all (mid-session
    // commits pile up in this._committed unseen, and the whole thing lands in one
    // write on stop). skips the write if the user edited the input field in the
    // meantime.
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
    },

    // drains the session: stops capture, waits for any in-flight commit, commits
    // the remaining open segment (the tail), releases the audio, and - for
    // dictation only (writeToInput) - writes the committed text into the input
    // field. shared by stopRecording (dictation) and stopMeetingRecording (meeting)
    async _finishCapture(writeToInput) {
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
            if (writeToInput && !this._committed && this.preview) {
                // salvage the last preview so a short utterance isn't lost
                this._committed = this.preview;
                this.preview = "";
                this._writeInput();
            }
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
                if (this.mode === "meeting" && this.onSegmentCommitted) {
                    this.onSegmentCommitted({ tsSec: tailTsSec, text });
                }
            } else if (writeToInput && !this._committed && this.preview) {
                this._committed = this.preview;
                this.preview = "";
            }
            if (writeToInput) {
                this._writeInput();
            }
        } catch (err) {
            if (this.mode === "meeting") {
                // the tail is the user's last words - never drop it silently. a
                // failed tail commit becomes a gap marker instead of an error.
                this.onGap && this.onGap({ tsSec: tailTsSec, index: (this._gapCount = (this._gapCount || 0) + 1) });
            } else {
                this.error = `Transcription failed: ${err}`;
                if (!this._committed && this.preview) {
                    this._committed = this.preview;
                    this.preview = "";
                    this._writeInput();
                }
            }
        } finally {
            this.transcribing = false;
        }

        return this._fullText();
    },

    // stops dictation capture and writes the committed text into the input field
    async stopRecording() {
        return this._finishCapture(true);
    },

    // stops meeting capture and commits the tail but NEVER writes the composer -
    // the meets store owns the transcript and renders it as its own message
    async stopMeetingRecording() {
        return this._finishCapture(false);
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
