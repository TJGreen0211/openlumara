let wsSocket = null;
let isWsConnected = false;
let wsReconnecting = false;
let wsReconnectTimer = null;
let responseSoundPlayed = false;

// exponential backoff for reconnects: a down server used to be hammered with
// a fresh connection attempt every second forever
let wsReconnectDelay = 1000;
const WS_RECONNECT_MAX_DELAY = 30000;

// a handshake that never completes (flaky mobile network) would otherwise sit in
// CONNECTING forever, and scheduleWsReconnect() refuses to run while it does
const WS_CONNECT_TIMEOUT = 10000;

// app-level heartbeat. browsers answer protocol pings without telling the page,
// and never time out a half-open socket themselves (iOS resuming a backgrounded
// tab is the classic case: readyState says OPEN but nothing ever arrives)
const WS_HEARTBEAT_INTERVAL = 25000;
const WS_PONG_TIMEOUT = 5000;
let wsHeartbeatTimer = null;
let wsPongTimer = null;
let wsPingId = 0;

// messages that must not be lost to a dropped connection (see queueSocketSend);
// sent in order as soon as the socket is open again
let sendQueue = [];

// handshakes that failed in a row: a reverse proxy can turn the server's 4001
// into a bare failure, so after a few we check whether the session is still valid
let wsFailedAttempts = 0;

async function connectWebSocket() {
    // a new connection attempt starts: re-arm the onclose/onerror guards
    wsReconnecting = false;
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

    const pathname = `${window.location.pathname || '/'}`;
    const pathBase = pathname.endsWith('/') ? pathname.slice(0, -1) : pathname;
    const wsPath = `${pathBase === '' ? '' : pathBase}/ws`;
    const wsUrl = `${wsProtocol}//${window.location.host}${wsPath}`;

    try {
        wsSocket = new WebSocket(wsUrl);

        // store a global reference of it
        window.socket = wsSocket;
    } catch (e) {
        await scheduleWsReconnect();
        return;
    }

    const thisSocket = wsSocket;

    const connectTimer = setTimeout(() => {
        if (thisSocket === wsSocket && thisSocket.readyState === WebSocket.CONNECTING) {
            console.log('WebSocket handshake timed out');
            forceWsReconnect();
        }
    }, WS_CONNECT_TIMEOUT);

    wsSocket.onopen = async () => {
        clearTimeout(connectTimer);
        console.log('WebSocket connected');
        Alpine.store('ui').connectionState = 'connected';
        isWsConnected = true;
        wsReconnecting = false;
        wsReconnectDelay = 1000;
        wsFailedAttempts = 0;
        startWsHeartbeat();
        flushSendQueue();

        // a send that failed against a dead socket can leave the stream state
        // wedged in message_sending; a fresh connection always starts idle
        // (the backend cancels this user's stream task on disconnect, so
        // there is no live stream to clobber here)
        // clear() also drops stream.turn/userMsg: a mid-stream drop would
        // otherwise leave a partial turn that the next turn_stream would
        // append to, or that would render as an orphaned bubble
        await Alpine.store('stream').clear();
        Alpine.store('stream').state = 'idle';

        await Alpine.store("chat").reloadChat();
    };

    wsSocket.onmessage = async (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'pong') {
                clearTimeout(wsPongTimer);
                wsPongTimer = null;
                return;
            }
            enqueueWsMessage(data);
        } catch (e) {
            console.error('Error parsing WebSocket message:', e);
        }
    };

    wsSocket.onclose = async (event) => {
        clearTimeout(connectTimer);
        // a stale socket closing after a newer one was created must not
        // clobber the live reference or schedule a parallel connection
        if (thisSocket !== wsSocket) {
            return;
        }
        if (!wsReconnecting) {
            console.log('WebSocket disconnected:', event.code, event.reason);
            markWsDisconnected();

            if (event.code === 4001) {
                // logged out / session expired: retrying can never succeed
                window.location.href = '/login';
                return;
            }
            if (event.code === 1006 && ++wsFailedAttempts >= 3 && await sessionExpired()) {
                window.location.href = '/login';
                return;
            }

            await scheduleWsReconnect();
        }
    };

    wsSocket.onerror = async (error) => {
        if (!wsReconnecting) {
            console.error('WebSocket error:', error);
        }
    };
}

async function sessionExpired() {
    // true only for a definite 401; a network failure is not an expired session
    try {
        const response = await fetch('/api/chat/current', { cache: 'no-store' });
        return response.status === 401;
    } catch (e) {
        return false;
    }
}

function queueSocketSend(data) {
    /*
     * like simpleSocketSend, but if the socket is down the message waits in
     * sendQueue and goes out on reconnect instead of being dropped (a meeting
     * transcript must survive the phone having locked its screen)
     */
    if (wsSocket && wsSocket.readyState === WebSocket.OPEN) {
        try {
            wsSocket.send(JSON.stringify(data));
            return true;
        } catch (e) {}
    }
    console.log(`WebSocket is down, queued '${data.type}' until it reconnects`);
    sendQueue.push(data);
    checkWsConnection();
    return false;
}

function flushSendQueue() {
    while (sendQueue.length && wsSocket && wsSocket.readyState === WebSocket.OPEN) {
        try {
            wsSocket.send(JSON.stringify(sendQueue[0]));
        } catch (e) {
            return;
        }
        sendQueue.shift();
    }
}

function markWsDisconnected() {
    stopWsHeartbeat();
    wsSocket = null;
    window.socket = null;
    isWsConnected = false;

    Alpine.store('ui').connectionState = 'disconnected';
    // the server keeps streaming without us; stream_resume restores the
    // turn on reconnect
    Alpine.store("stream").state = 'idle';
}

function forceWsReconnect() {
    /*
     * drop the current socket and reconnect right away. close() on a dead
     * socket starts a close handshake the server never answers, so onclose
     * can take a minute or more (Safari) - detach it instead of waiting
     */
    const old = wsSocket;
    if (old) {
        old.onopen = old.onmessage = old.onclose = old.onerror = null;
        try { old.close(); } catch (e) {}
        markWsDisconnected();
    }
    wsReconnectDelay = 1000;
    connectWebSocket();
}

function startWsHeartbeat() {
    stopWsHeartbeat();
    wsHeartbeatTimer = setInterval(() => {
        // background tabs get their timers throttled anyway; the wake
        // handlers below check the socket the moment the page is visible
        if (document.visibilityState === 'visible') {
            pingWs();
        }
    }, WS_HEARTBEAT_INTERVAL);
}

function stopWsHeartbeat() {
    clearInterval(wsHeartbeatTimer);
    clearTimeout(wsPongTimer);
    wsHeartbeatTimer = null;
    wsPongTimer = null;
}

function pingWs(timeout = WS_PONG_TIMEOUT) {
    if (!wsSocket || wsSocket.readyState !== WebSocket.OPEN || wsPongTimer) {
        return;
    }
    const id = ++wsPingId;
    try {
        wsSocket.send(JSON.stringify({ type: 'ping', id: id }));
    } catch (e) {
        forceWsReconnect();
        return;
    }
    wsPongTimer = setTimeout(() => {
        wsPongTimer = null;
        console.log('WebSocket heartbeat missed, reconnecting');
        forceWsReconnect();
    }, timeout);
}

function checkWsConnection() {
    /*
     * the page came back (tab shown, restored from the back/forward cache, or
     * the network returned). don't wait out a backed-off reconnect timer that
     * may also have been frozen while the page was hidden
     */
    wsReconnectDelay = 1000;
    if (!wsSocket || wsSocket.readyState === WebSocket.CLOSED || wsSocket.readyState === WebSocket.CLOSING) {
        forceWsReconnect();
    } else if (wsSocket.readyState === WebSocket.OPEN) {
        // probably fine, but a resumed iOS page can hold a half-open socket
        pingWs(3000);
    }
    // CONNECTING: the handshake timeout covers it
}

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        checkWsConnection();
    }
});
window.addEventListener('pageshow', (event) => {
    if (event.persisted) {
        checkWsConnection();
    }
});
window.addEventListener('online', checkWsConnection);

async function scheduleWsReconnect() {
    // guard against onclose/onerror of the same socket stacking attempts;
    // cleared at the start of the next connectWebSocket()
    if (wsReconnecting) {
        return;
    }
    // a live connection already exists (an older socket's onclose must not
    // spawn a second one alongside it)
    if (window.socket && (window.socket.readyState === WebSocket.OPEN || window.socket.readyState === WebSocket.CONNECTING)) {
        return;
    }
    wsReconnecting = true;

    const delay = wsReconnectDelay;
    wsReconnectDelay = Math.min(wsReconnectDelay * 2, WS_RECONNECT_MAX_DELAY);

    console.log(`attempting to reconnect to websocket in ${delay}ms..`);
    wsReconnectTimer = setTimeout(function () {
        connectWebSocket();
    }, delay);
}

/*
 * handlers run one at a time, in arrival order. they await HTTP reloads, and
 * running them concurrently let a slow user_message_added (waiting on
 * reloadChat) finish after stream_complete and wedge the UI in message_sending.
 * a handler stuck on a dead request only holds the queue for so long
 */
const WS_HANDLER_TIMEOUT = 15000;
let wsHandlerChain = Promise.resolve();

function enqueueWsMessage(data) {
    wsHandlerChain = wsHandlerChain.then(() => {
        let timer;
        const timeout = new Promise((resolve) => {
            timer = setTimeout(() => {
                console.warn(`WebSocket handler for '${data.type}' is taking too long, moving on`);
                resolve();
            }, WS_HANDLER_TIMEOUT);
        });
        const handled = handleWebSocketMessage(data).catch((e) => {
            console.error(`Error handling WebSocket message '${data.type}':`, e);
        });
        return Promise.race([handled, timeout]).finally(() => clearTimeout(timer));
    });
}

async function handleWebSocketMessage(data) {
    // we store all the stream-related data in an Alpine store
    const stream = Alpine.store("stream");
    const chat = Alpine.store("chat");
    const ui = Alpine.store('ui');

    if (data.type != 'token' && data.type != "turn_stream" && data.type != "turn_delta") {
        console.log(data);
    }

    // process based on broadcast type
    switch (data.type) {
        case "sync_state":
            /*
             * use the token buffer from the backend
             * to load into the frontend
             *
             * fun lil comment about this... this used to take me ages to get right in the old webUI
             * i had to basically simulate a replay of the entire token stream
             * here it's just a simple assignment!
             * thanks alpine.js
             */

            // actually nope its not even needed anymore because we just stream an entire turn from the backend LOL
            // this is now just here for a memory, this data type doesn't actually get broadcast anymore
            break;

        case "user_message_added":
            // the backend received the message; cancel the send-ack watchdog
            chat._clearSendAck();

            // reload chat from backend so that the new user message shows up
            await chat.reloadChat();

            // force scroll to bottom
            await ui.forceScrollToBottom();

            stream.state = 'message_sending';
            break;

        case "user_message_confirmed":
            stream.state = 'message_received';
            break;

        case "stream_resume":
            // we reconnected while the backend was still streaming (phone came
            // back from the background, network blip), or asked for a resync:
            // pick the turn back up. history past the user message is left out,
            // the backend already saved this stream's tool call messages and
            // the bubble shows them
            streamResyncRequested = false;
            chat._clearSendAck();
            stream.cutoffIndex = data.index;
            stream.turn = data.turn.messages.length ? data.turn : [];
            stream.state = data.state;
            await chat.reloadChat();
            await ui.scrollToBottom();
            break;

        case "error":
            // top-level command errors (e.g. oversized uploads, failed regenerate).
            // no stream was started, so unstick the UI instead of waiting on the
            // send-ack watchdog to recover
            chat._clearSendAck();
            stream.state = 'idle';
            Alpine.store('notifications').send(data.error || 'Unknown error');
            alert(data.error || 'Unknown error');
            break;

        case "turn_stream": {
            // a new segment, or a full replacement of the current one
            const segment = data.turn;

            if (!stream.turn.messages) {
                stream.turn = {role: "assistant", messages: []};
            }

            // check if this updates the last segment or starts a new one
            const last_idx = stream.turn.messages.length - 1;
            const last_segment = stream.turn.messages[last_idx];

            // same type and same tool_call_id = update existing segment
            const is_update = last_segment &&
                              last_segment.type === segment.type &&
                              last_segment.tool_call_id === segment.tool_call_id;

            if (is_update) {
                // backend sends FULL accumulated content, so replace in place
                stream.turn.messages[last_idx] = segment;
            } else {
                // new segment type (or new tool response), push it
                stream.turn.messages.push(segment);
            }

            await afterTurnUpdate(stream, chat, ui);
            break;
        }

        case "turn_delta": {
            // growing text segments arrive as appends at a known offset
            // (UTF-16 units, same as JS string length)
            const msgs = stream.turn.messages;
            const seg = msgs && msgs[msgs.length - 1];
            const current = seg ? (seg[data.key] || '') : null;

            if (current === null || current.length < data.offset) {
                // we missed an update (or have no turn at all): get a snapshot
                requestStreamResync();
                break;
            }
            if (current.length < data.offset + data.append.length) {
                seg[data.key] = current.slice(0, data.offset) + data.append;
            }
            // otherwise we already have it (a resume snapshot raced ahead)

            await afterTurnUpdate(stream, chat, ui);
            break;
        }

        case "push":
            // it's a push messsage (like a scheduler reminder)
            chat.turnHistory.push(data.content);
            await chat.reloadChat();

            console.log(data.content);
            await AudioManager.play('response_start');
            await Alpine.store('notifications').send(data.content.content);

            await ui.forceScrollToBottom();
            break;

        case "log":
            Alpine.store('system').logs.push(data);
            break;

        case "ready": {
            const sys = Alpine.store("system");
            sys.running = true;
            sys.restarting = false;

            // reload the system data from the backend
            await sys.loadData();
            break;
        }

        case "shutdown": {
            const sys = Alpine.store("system");
            if (!sys.restarting) {
                sys.message = "Server is down! Trying to reconnect..";
                sys.running = false;
            }
            break;
        }

        case "token": {
            const token = data.content;
            const token_content = token.content;

            // process tokens based on their type. content/reasoning tokens are
            // not sent: their text arrives through turn_stream / turn_delta
            switch (token.type) {
                case "error":
                    // force a refresh
                    await chat.reloadChat();
                    break;
                case "tool":
                    stream.state = 'processing_tools';
                    AudioManager.playProcessingSound();
                    break;
                case "prompt_progress":
                    if (stream.state != "processing_tools") {
                        // let it stay in that state if it's been set
                        stream.state = 'processing';
                    }

                    stream.processing = token_content;

                    AudioManager.playProcessingSound();
                    break;
                case "token_usage":
                    chat.currentTokenUsage = token_content;
                    break;
                case "tool_call_delta":
                    stream.state = 'calling_tools';
                    stream.processing = {};
                    break;
                case "tool_calls":
                    stream.state = 'calling_tools';
                    break;
            }

            // always scroll to the bottom upon a token coming in
            await ui.scrollToBottom();

            break;
        }

        case "sync":
            // make sure we sync chat
            chat.turnHistory = [];
            await stream.clear();
            await chat.reloadChat();

            if (ui.scrollToTurnIndex) {
                await ui.scrollToTurn(ui.scrollToTurnIndex);
            } else {
                await ui.scrollToBottom();
            }

            break;

        case "chat_switched":
            // make sure we sync chat switches across instances
            await chat.loadChat(data.id);
            break;

        case "chat_metadata_updated":
            // rename or meeting-room toggle (from this tab or another one):
            // refresh the open chat's metadata/title in place, then the sidebar
            if (data.id && data.id !== chat.selectedChat) {
                // another chat changed - only the sidebar list carries it
                await chat.reloadChats();
                break;
            }
            if (data.title) { chat.chat.title = data.title; }
            if (data.tags) { chat.chat.tags = data.tags; }
            if (data.metadata) { chat.chat.metadata = data.metadata; }
            await chat.reloadChats();
            break;

        case "stream_complete":
            /*
             * this prevents UI flicker that comes from re-rendering the entire turn history
             */
            streamResyncRequested = false;
            responseSoundPlayed = false;

            // push the fully collected assistant turn to the history (the user
            // message is already there: user_message_added reloaded the chat)
            if (stream.turn.messages && stream.turn.messages.length) {
                chat.turnHistory.push(stream.turn);
            }

            // then clear the current turn
            await stream.clear();

            // finalize the stream
            AudioManager.play("completion");
            await ui.scrollToBottom();

            // and finally, sync back up with the backend
            await chat.reloadChat();
            await chat.reloadChats();

            stream.state = 'idle';

            // the turn's file writes have landed, so open a preview that was
            // only recorded mid-stream and refresh one that's already open
            Alpine.store('artifact').finalize();

            break;
    }
}

let streamResyncRequested = false;

function requestStreamResync() {
    // one outstanding request at a time; stream_resume / stream_complete clear it
    if (streamResyncRequested) {
        return;
    }
    streamResyncRequested = true;
    simpleSocketSend({ type: 'resync' }).then((sent) => {
        if (!sent) {
            streamResyncRequested = false;
        }
    });
}

async function afterTurnUpdate(stream, chat, ui) {
    // shared tail of turn_stream / turn_delta: scroll, state, sounds, live previews
    await ui.scrollToBottom();

    // handle state based on the current segment
    const current_segment = stream.turn.messages[stream.turn.messages.length - 1];

    switch (current_segment.type) {
        case "reasoning":
            stream.state = 'thinking';
            stream.processing = {};
            AudioManager.stopProcessingSound();
            AudioManager.play("token");
            if (!responseSoundPlayed) {
                AudioManager.play("response_start");
                responseSoundPlayed = true;
            }
            break;
        case "content":
            stream.state = 'streaming';
            stream.processing = {};
            AudioManager.stopProcessingSound();
            AudioManager.play("token");
            if (!responseSoundPlayed) {
                AudioManager.play("response_start");
                responseSoundPlayed = true;
            }
            break;
    }

    if (current_segment.is_cmd) {
        await chat.reloadChats();
        await chat.reloadCategories();
    }

    // track live-preview sites as the AI touches HTML files. tool calls
    // stream BEFORE the server executes them, so file writes from this
    // turn aren't on disk yet: file tools only *record* the site, and
    // stream_complete opens the panel once the writes have landed.
    // open_preview opens it immediately, since the file already exists
    // by the time the model calls it. the store only accepts a call once
    // its arguments are complete JSON
    if (current_segment.type === 'tool_calls' && Array.isArray(current_segment.tool_calls)) {
        current_segment.tool_calls.forEach((tc, i) => {
            Alpine.store('artifact').recordToolCall(tc, tc.id || `${stream.turn.messages.length}:${i}`);
        });
    }
}
