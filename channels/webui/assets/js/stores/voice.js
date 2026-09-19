/*
 * voice input store
 *
 * captures microphone audio as raw PCM in memory (audio worklet, no files ever touch disk)
 * and transcribes it in VAD-split segments:
 *
 * - while the user is talking, the last few seconds of the open segment are sent as a
 *   live preview (purpose "preview") and shown as a flickering tail in the input field
 * - when the user pauses (or a segment gets long enough), the finished segment is
 *   transcribed (purpose "commit") and appended to the committed text, which is never
 *   rewritten again
 * - on stop, the remaining open segment is committed the same way
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

    _ctx: null,
    _stream: null,
    _source: null,
    _node: null,
    _workletUrl: null,

    _pcmBuf: null,
    _pcmLength: 0,
    _rate: 16000,

    _session: 0,
    _prefix: "",
    _committed: "",
    _preview: "",
    _lastWritten: "",

    // the open segment starts at sample _segStart; _lastVoiced tracks the last voiced sample
    _segStart: 0,
    _lastVoiced: -1,
    _voicedSinceSend: false,

    _tickTimer: null,
    _tickInFlight: false,
    _commitInFlight: null,
    _previewFails: 0,

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
        return iphony && /Safari|CriOS|FxiOS/.test(ua);
    },

    async startRecording() {
        if (this.recording) {
            return true;
        }
        this.error = null;
        if (!this.isSupported()) {
            this.error = "Voice input is not supported in this browser.";
            return false;
        }

        const chat = Alpine.store("chat");
        this._prefix = chat ? (chat.user_input || "").trim() : "";
        this._lastWritten = chat ? (chat.user_input || "") : "";
        this._committed = "";
        this._preview = "";
        this._segStart = 0;
        this._lastVoiced = -1;
        this._voicedSinceSend = false;
        this._previewFails = 0;
        this._commitInFlight = null;

        try {
            this._stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true }
            });
        } catch (err) {
            this.error = err.name === "NotAllowedError"
                ? "Microphone access was denied. Please allow microphone access in your browser settings."
                : `Failed to start recording: ${err.message}`;
            return false;
        }

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

    _appendPcm(chunk) {
        if (this._pcmBuf.length - this._pcmLength < chunk.length) {
            const bigger = new Float32Array(Math.max(this._pcmBuf.length * 2, this._pcmLength + chunk.length));
            bigger.set(this._pcmBuf);
            this._pcmBuf = bigger;
        }
        this._pcmBuf.set(chunk, this._pcmLength);
        this._pcmLength += chunk.length;

        // track the chunk's energy for silence detection (segment splitting)
        let sum = 0;
        for (let i = 0; i < chunk.length; i++) {
            const s = chunk[i];
            sum += s * s;
        }
        this._vadUpdate(Math.sqrt(sum / chunk.length), (chunk.length / this._rate) * 1000);
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
        const threshold = Math.max(this.vadAbsMin, vad.floor * this.vadFloorFactor);

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
                this._segStart = newSegStart;
                if (this.recording) {
                    this._vad = this._newVad();
                }
            }
            this._commitInFlight = null;
        });
    },

    // transcribes one finished segment and appends the result to the committed text
    async _commitSegment(fromSample, toSample) {
        const session = this._session;
        const rate = this._rate;

        if (toSample - fromSample < rate * 0.4) {
            // too short to be worth transcribing
            return;
        }

        const pcm = this._pcmBuf.slice(fromSample, toSample); // copy; the buffer keeps growing
        this.transcribing = true;
        try {
            const wav = pcmToWavBase64(pcm, rate, this.targetRate);
            const result = await simpleApiPost("/api/voice/transcribe", {
                audio_data: wav,
                format: "wav",
                purpose: "commit"
            });
            const text = (result && result.text) ? String(result.text).trim() : "";
            // only a brand-new recording invalidates this commit
            if (session !== this._session) {
                return;
            }
            if (text) {
                this._committed = this._committed ? `${this._committed} ${text}`.trim() : text;
                this._previewFails = 0;
            }
            this._writeInput();
        } catch (err) {
            if (session !== this._session) {
                return;
            }
            this.error = `Transcription failed: ${err}`;
        } finally {
            if (session === this._session) {
                this.transcribing = false;
            }
        }
    },

    // sends the most recent window of the open segment as a live preview and replaces the tail
    _tickPreview(session) {
        if (!this.recording || session !== this._session || this._tickInFlight || this._commitInFlight) {
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
                if (text && text !== this._preview) {
                    this._preview = text;
                    this._writeInput();
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
        if (this._committed && this._preview) {
            return `${this._committed} ${this._preview}`.trim();
        }
        return this._committed || this._preview;
    },

    // writes text into the chat input, preserving the pre-recording text.
    // skips the write if the user edited the input field in the meantime.
    _writeInput() {
        const chat = Alpine.store("chat");
        if (!chat) {
            return;
        }
        if (chat.user_input !== this._lastWritten) {
            return;
        }
        const text = this._fullText();
        const next = this._prefix ? `${this._prefix} ${text}`.trim() : text;
        chat.user_input = next;
        this._lastWritten = next;
    },

    // stops recording and commits the remaining open segment; resolves with the final text
    async stopRecording() {
        if (!this.recording) {
            return this._fullText();
        }
        const session = this._session;
        this.recording = false;
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
        const pcm = this._pcmBuf ? this._pcmBuf.slice(this._segStart, this._pcmLength) : new Float32Array(0);

        this._releaseAudio();
        this._pcmBuf = null;
        this._pcmLength = 0;

        if (pcm.length < rate * 0.4) {
            // nothing meaningful left in the open segment
            if (!this._committed && this._preview) {
                // salvage the last preview so a short utterance isn't lost
                this._committed = this._preview;
                this._preview = "";
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
            } else if (!this._committed && this._preview) {
                this._committed = this._preview;
                this._preview = "";
            }
            this._writeInput();
        } catch (err) {
            this.error = `Transcription failed: ${err}`;
            if (!this._committed && this._preview) {
                this._committed = this._preview;
                this._preview = "";
                this._writeInput();
            }
        } finally {
            this.transcribing = false;
        }

        return this._fullText();
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
