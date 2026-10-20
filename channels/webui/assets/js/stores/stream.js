let STREAM_STORE = {
    // one of: idle, sending, processing, streaming
    state: 'idle',

    // stores raw token data
    turn: [],
    processing: {},

    // stores the final message after the stream has finished
    finalMessage: [],

    // message index of a resumed stream's user message: history turns after it
    // are hidden while the stream runs (see chat._withoutActiveStream)
    cutoffIndex: null,

    async clear() {
        this.turn = [];
        this.userMsg = null;
        this.processing = {};
        this.cutoffIndex = null;
    }
}
