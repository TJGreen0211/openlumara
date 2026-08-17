const VOICE_STORE = {
    recording: false,
    transcribing: false,
    error: null,
    _recorder: null,
    _chunks: [],
    _stream: null,

    isSupported() {
        return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    },

    async startRecording() {
        this.error = null;
        if (!this.isSupported()) {
            this.error = "Voice input is not supported in this browser.";
            return false;
        }
        try {
            this._stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            this._chunks = [];
            const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
                ? "audio/webm;codecs=opus"
                : MediaRecorder.isTypeSupported("audio/webm")
                    ? "audio/webm"
                    : "";
            this._recorder = new MediaRecorder(this._stream, mimeType ? { mimeType } : {});
            this._recorder.ondataavailable = (e) => { if (e.data.size > 0) this._chunks.push(e.data); };
            this._recorder.start(100);
            this.recording = true;
            return true;
        } catch (err) {
            this.error = err.name === "NotAllowedError"
                ? "Microphone access was denied. Please allow microphone access in your browser settings."
                : `Failed to start recording: ${err.message}`;
            return false;
        }
    },

    async stopRecording() {
        return new Promise((resolve) => {
            if (!this._recorder) {
                resolve(null);
                return;
            }
            // Capture refs before nulling — onstop fires asynchronously
            const recorder = this._recorder;
            const chunks = this._chunks;
            const stream = this._stream;
            const mimeType = recorder.mimeType || "audio/webm";

            recorder.onstop = async () => {
                const blob = new Blob(chunks, { type: mimeType });
                const format = blob.type.split("/")[1]?.split(";")[0] || "webm";
                const reader = new FileReader();
                reader.onload = () => {
                    const b64 = reader.result.split(",")[1];
                    resolve({ data: b64, format });
                };
                reader.readAsDataURL(blob);
            };
            recorder.stop();
            this.recording = false;
            if (stream) {
                stream.getTracks().forEach(t => t.stop());
            }
            this._stream = null;
            this._recorder = null;
            this._chunks = [];
        });
    }
};
