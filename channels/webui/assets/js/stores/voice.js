/*
 * voice input store
 *
 * captures microphone audio as raw PCM in memory (audio worklet, no files ever touch disk),
 * and shows a live rolling-window transcription preview in the chat input while the user speaks:
 * every couple of seconds the last ~6s of audio is sent to the voice endpoint and the input
 * field is replaced with that result.
 * the microphone auto-stops after the captured audio has been silent for long enough
 * (lightweight energy-based VAD on the PCM, no extra dependencies). on stop, the full
 * recording is transcribed and the final text is committed to the input.
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

    _previewTimer: null,
    _previewInFlight: false,
    _session: 0,

    // energy vad state: { floor: noise level, silentMs, speechMs }
    _vad: null,

    // text in the input field before recording started, and what this store last wrote
    _prefix: "",
    _lastWritten: "",

    // how much audio to resample/encode at: whisper's native rate keeps uploads small
    targetRate: 16000,
    previewWindowSec: 6,
    previewIntervalMs: 2000,
    minPreviewSec: 1.5,

    // auto-stop: end the recording after this much consecutive silence, but only once at
    // least minSpeechMs of speech has been heard (so a fresh recording doesn't end itself).
    // keep this long enough that a thinking pause doesn't cut the capture off
    silenceStopMs: 2000,
    minSpeechMs: 800,
    // silence threshold: max(vadAbsMin, noise floor * vadFloorFactor), the floor adapts
    vadFloorFactor: 3.0,
    vadAbsMin: 0.006,

    isSupported() {
        const hasContext = !!(window.AudioContext || window.webkitAudioContext);
        return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && hasContext);
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
            this._vad = { floor: 0.01, silenceMs: 0, speechMs: 0 };

            this._source = this._ctx.createMediaStreamSource(this._stream);
            this._node = new AudioWorkletNode(this._ctx, "voice-capture-processor");
            this._node.port.onmessage = (e) => this._appendPcm(e.data);
            this._source.connect(this._node);

            this.recording = true;
            this._session++;
            this._previewInFlight = false;

            this._previewTimer = setInterval(
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

    _appendPcm(chunk) {
        if (this._pcmBuf.length - this._pcmLength < chunk.length) {
            const bigger = new Float32Array(Math.max(this._pcmBuf.length * 2, this._pcmLength + chunk.length));
            bigger.set(this._pcmBuf);
            this._pcmBuf = bigger;
        }
        this._pcmBuf.set(chunk, this._pcmLength);
        this._pcmLength += chunk.length;

        // track the chunk's energy for silence detection (auto stop)
        let sum = 0;
        for (let i = 0; i < chunk.length; i++) {
            const s = chunk[i];
            sum += s * s;
        }
        const rms = Math.sqrt(sum / chunk.length);
        this._vadUpdate(rms, (chunk.length / this._rate) * 1000);
    },

    // lightweight energy vad: ends the recording once the user has been silent long enough,
    // but only after at least minSpeechMs of speech has been heard
    _vadUpdate(rms, ms) {
        if (!this.recording || !this._vad) {
            return;
        }

        // low-envelope noise floor: snaps down to new quiet levels, drifts up very slowly
        const vad = this._vad;
        vad.floor = rms < vad.floor ? rms : Math.min(0.02, vad.floor * 1.002);
        const threshold = Math.max(this.vadAbsMin, vad.floor * this.vadFloorFactor);

        if (rms >= threshold) {
            // speech
            vad.speechMs += ms;
            vad.silenceMs = 0;
            return;
        }

        if (vad.speechMs < this.minSpeechMs) {
            // no speech heard yet, don't auto-stop a fresh recording
            return;
        }

        vad.silenceMs += ms;
        if (vad.silenceMs >= this.silenceStopMs) {
            this._vad = null; // prevent re-trigger from in-flight chunks
            this.stopRecording();
        }
    },

    // sends the most recent window of audio through the voice endpoint and replaces the input text
    _tickPreview(session) {
        if (!this.recording || session !== this._session || this._previewInFlight) {
            return;
        }

        const rate = this._rate;
        const totalSec = this._pcmLength / rate;
        if (totalSec < this.minPreviewSec) {
            return;
        }

        // take the last window of audio (capped at everything recorded so far)
        const windowSamples = Math.floor(Math.min(this.previewWindowSec, totalSec) * rate);
        const pcm = this._pcmBuf.slice(this._pcmLength - windowSamples, this._pcmLength);

        let wav;
        try {
            wav = pcmToWavBase64(pcm, rate, this.targetRate);
        } catch (err) {
            return;
        }

        this._previewInFlight = true;
        simpleApiPost("/api/voice/transcribe", { audio_data: wav, format: "wav" })
            .then((result) => {
                // ignore stale responses (recording stopped, or a new one started)
                if (session !== this._session || !this.recording) {
                    return;
                }
                const text = (result && result.text) ? String(result.text).trim() : "";
                this._applyText(text);
            })
            .catch(() => {
                // preview failures are non-fatal, the final pass on stop still happens
            })
            .finally(() => {
                this._previewInFlight = false;
            });
    },

    // stops recording and transcribes the full recording; resolves with the final text
    async stopRecording() {
        if (!this.recording) {
            return "";
        }
        this._session++;
        const session = this._session;
        this.recording = false;
        this._vad = null;

        if (this._previewTimer) {
            clearInterval(this._previewTimer);
            this._previewTimer = null;
        }

        const rate = this._rate;
        const pcm = this._pcmBuf ? this._pcmBuf.slice(0, this._pcmLength) : new Float32Array(0);

        this._releaseAudio();
        this._pcmBuf = null;
        this._pcmLength = 0;

        if (pcm.length / rate < 0.4) {
            // too short to be worth transcribing, drop the preview
            if (session === this._session) {
                this._applyText("");
            }
            return "";
        }

        this.transcribing = true;
        try {
            const wav = pcmToWavBase64(pcm, rate, this.targetRate);
            const result = await simpleApiPost("/api/voice/transcribe", { audio_data: wav, format: "wav" });
            const text = (result && result.text) ? String(result.text).trim() : "";
            if (session === this._session) {
                this._applyText(text);
                return text;
            }
            return "";
        } catch (err) {
            // keep the last preview in the input field, it's the best we have
            if (session === this._session) {
                this.error = `Transcription failed: ${err}`;
            }
            return "";
        } finally {
            if (session === this._session) {
                this.transcribing = false;
            }
        }
    },

    // writes text into the chat input, preserving the pre-recording text.
    // skips the write if the user edited the input field in the meantime.
    _applyText(text) {
        const chat = Alpine.store("chat");
        if (!chat) {
            return;
        }
        if (chat.user_input !== this._lastWritten) {
            return;
        }
        const next = this._prefix ? `${this._prefix} ${text || ""}`.trim() : (text || "");
        chat.user_input = next;
        this._lastWritten = next;
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
