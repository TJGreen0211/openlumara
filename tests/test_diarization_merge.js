/*
 * Self-contained unit test for the meeting speaker-diarization logic in
 * channels/webui/assets/js/stores/meets.js.
 *
 * It loads meets.js in a fresh vm context (like a <script> tag) with lightweight
 * stubs for the browser / app globals, then verifies:
 *   1. olDiagAssignSpeakers (the pure midpoint/overlap merge) - boundary, trailing,
 *      and gap-fallback cases
 *   2. the startDiarization latch (a rapid 2nd click is a no-op)
 *   3. the "no configured URL" guard (nothing reaches the network)
 *
 * Run:  node tests/test_diarization_merge.js
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const MEETS_PATH = path.join(__dirname, "..", "channels", "webui", "assets", "js", "stores", "meets.js");
const code = fs.readFileSync(MEETS_PATH, "utf8");

// --- mutable test hooks (shared with the vm context) -------------------------
const cfg = { url: "" };
let postCalls = 0;
let socketSends = [];
let lastPostBody = null;

// --- browser / app stubs (only what the store actually references) -----------
const stubs = {
    console,
    setTimeout,
    setInterval,
    clearTimeout,
    clearInterval,
    URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
    window: { addEventListener() {}, removeEventListener() {}, isSecureContext: true, socket: { send: () => true } },
    document: {
        addEventListener() {},
        removeEventListener() {},
        createElement: () => ({ set href(v) {}, set download(v) {}, click() {} }),
        body: { appendChild() {}, removeChild() {} },
    },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    crypto: { randomUUID: () => "00000000-0000-0000-0000-000000000000" },
    Alpine: {
        store(name) {
            if (name === "settings") {
                return {
                    settings: {
                        channels: { settings: { webui: { diarization_server_url: cfg.url, stt_whisper_server_url: "http://stt" } } },
                        api: { voice_url: "http://stt" }
                    }
                };
            }
            if (name === "stream") return { state: "idle" };
            if (name === "voice") {
                return {
                    isSupported: () => true,
                    recording: false,
                    meetingElapsedSec: () => 0,
                    meetingVadAbsMin: 0,
                    startRecording: async () => true,
                    stopMeetingRecording: async () => {},
                    pauseCapture: async () => {},
                    resumeCapture: async () => {}
                };
            }
            return {};
        }
    },
    RecordingStore: {
        getMeeting: async () => ({
            codec: "audio/webm;codecs=opus",
            title: "Meeting Test",
            fragments: [
                { tsMs: 1, blob: { arrayBuffer: async () => new ArrayBuffer(8) }, codec: "audio/webm;codecs=opus", title: "Meeting Test" },
                { tsMs: 2, blob: { arrayBuffer: async () => new ArrayBuffer(8) }, codec: "audio/webm;codecs=opus", title: "Meeting Test" }
            ]
        }),
        deleteMeeting: () => 1,
        saveFragment: async () => ({})
    },
    simpleApiPost: async function (url, body) {
        postCalls++;
        lastPostBody = body;
        return { job: "j1" };
    },
    simpleApiFetch: async function () {
        return { state: "processing" };
    },
    simpleSocketSend: function (data) {
        socketSends.push(data);
        return true;
    },
    arrayBufferToBase64: (buf) => "BASE64"
};

// run meets.js as a classic script in its own context
vm.createContext(stubs);
vm.runInContext(code, stubs, { filename: "meets.js" });
vm.runInContext("globalThis.__export = { store: MEETS_STORE, assign: olDiagAssignSpeakers };", stubs);

const { store, assign } = stubs.__export;

// element-wise array compare (cross-realm-safe: primitives only)
function arrEq(actual, expected, label) {
    assert.ok(Array.isArray(actual), label + ": not an array");
    assert.equal(actual.length, expected.length, label + ": length " + actual.length + " != " + expected.length);
    for (let i = 0; i < expected.length; i++) {
        assert.equal(actual[i], expected[i], label + "[" + i + "] = " + JSON.stringify(actual[i]) + " expected " + JSON.stringify(expected[i]));
    }
}

async function flush() {
    return new Promise((r) => setImmediate(r));
}

let failures = 0;
function check(name, fn) {
    return Promise.resolve()
        .then(fn)
        .then(() => console.log("  ok  " + name))
        .catch((e) => {
            failures++;
            console.log("  FAIL " + name);
            console.log("       " + (e && e.stack ? e.stack.split("\n").slice(0, 4).join("\n       ") : e));
        });
}

(async function main() {
    console.log("olDiagAssignSpeakers (midpoint/overlap merge):");
    await check("boundary / trailing / normal assignment", () => {
        const labels = [
            [0, 10, "A"],
            [10, 20, "B"],
            [20, 40, "C"]
        ];
        const segments = [
            { tsSec: 2, text: "a1" },
            { tsSec: 5, text: "a2" },
            { tsSec: 9.9, text: "a3" },      // mid 10.0 -> B (the takeover at 10.0 wins)
            { tsSec: 10.1, text: "b1" },
            { tsSec: 19.9, text: "c?" },     // mid 24.95 -> C
            { tsSec: 30, text: "c1" },
            { tsSec: 39, text: "tile" }      // trailing -> uses durationSec
        ];
        arrEq(assign(labels, segments, 40), ["A", "A", "B", "B", "C", "C", "C"], "merge");
    });

    await check("falls back to max overlap when midpoint is in an unlabeled gap", () => {
        const labels = [
            [0, 3, "A"],
            [8, 20, "B"]
        ];
        // only segment -> [2, durationSec=10), midpoint 6.0 falls in the 3..8 gap,
        // so the max-overlap pass decides: B overlaps [2,10) by 2s, A by 1s -> B
        const segments = [{ tsSec: 2, text: "x" }];
        arrEq(assign(labels, segments, 10), ["B"], "gap-fallback");
    });

    await check("returns all null for empty labels / empty segments", () => {
        arrEq(assign([], [{ tsSec: 1, text: "x" }], 10), [null], "no labels");
        arrEq(assign([[0, 5, "A"]], [], 10), [], "no segments");
    });

    console.log("startDiarization latch (re-click):");
    // reset hooks
    postCalls = 0;
    socketSends = [];
    cfg.url = "http://127.0.0.1:8001";
    store.meetingId = "m1";
    store.segments = [{ tsSec: 1, text: "hello" }];
    store.gaps = [];
    store.markers = [];
    store.durationSec = 5;
    store.title = "Meeting Test";
    store.diarizing = false;
    store.diarized = false;

    await check("a rapid second click is latched (only one job fires)", async () => {
        assert.equal(store.diarizeEnabled(), true, "should be enabled with a URL + meeting + segments");

        let resolvePost;
        const postPromise = new Promise((r) => { resolvePost = r; });
        const origPost = stubs.simpleApiPost;
        stubs.simpleApiPost = async function (url, body) { postCalls++; lastPostBody = body; return postPromise; };

        store.startDiarization();  // 1st: in-flight
        store.startDiarization();  // 2nd + 3rd: latched
        store.startDiarization();
        await flush();
        assert.equal(postCalls, 1, "exactly one diarize job should be submitted");

        resolvePost({ job: "j1" });  // let the first call finish (it starts the poll)
        await flush();
        store._stopDiarizePoll();    // clear the poll interval so the process can exit
        stubs.simpleApiPost = origPost;

        assert.ok(store.diarizing, "diarizing stays true once a job is in flight");
        assert.equal(store._diagJob, "j1", "job id was captured");
        const body = lastPostBody;
        assert.ok(Array.isArray(body.parts) && body.parts.length === 2, "sent 2 base64 parts");
        assert.equal(body.codec, "audio/webm;codecs=opus", "sent the codec");
    });

    console.log("'no configured URL' guard (no network):");
    await check("nothing reaches the network when no URL is configured", async () => {
        store.diarizing = false;
        store.diarized = false;
        store.error = null;
        postCalls = 0;
        socketSends = [];

        cfg.url = "";
        assert.equal(store.diarizeEnabled(), false, "feature is inert with no URL");

        // reset RecordingStore.getMeeting to a spy that must NOT be called
        const origGet = stubs.RecordingStore.getMeeting;
        let getCalled = false;
        stubs.RecordingStore.getMeeting = async () => { getCalled = true; return { fragments: [] }; };

        await store.startDiarization();
        stubs.RecordingStore.getMeeting = origGet;

        assert.equal(postCalls, 0, "simpleApiPost was never called");
        assert.equal(getCalled, false, "never tried to read the recording");
        assert.ok(store.error && /No diarization server configured/.test(store.error), "an explanatory error was set");
        assert.equal(store.diarizing, false, "cleaned up, no stuck state");
        assert.equal(store.diarized, false, "never marked diarized");
    });

    console.log(failures === 0 ? "\nAll diarization merge/latch/guard tests passed." : "\n" + failures + " test(s) FAILED.");
    process.exit(failures === 0 ? 0 : 1);
})();
