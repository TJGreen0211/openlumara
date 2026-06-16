let wsSocket = null;
let fancyProcessingIndicatorCreated = false;
let catchingUpFromBuffer = false;

function connectWebSocket() {
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = window.apiToken || '';
    const tokenParam = token ? `?token=${encodeURIComponent(token)}` : '';

    const pathname = `${window.location.pathname || '/'}`;
    const pathBase = pathname.endsWith('/') ? pathname.slice(0, -1) : pathname;
    const wsPath = `${pathBase === '' ? '' : pathBase}/ws`;
    const wsUrl = `${wsProtocol}//${window.location.host}${wsPath}${tokenParam}`;

    try {
        wsSocket = new WebSocket(wsUrl);
        window.socket = wsSocket;  // Keep global reference for send.js
    } catch (e) {
        console.error('Failed to create WebSocket:', e);
        scheduleWsReconnect();
        return;
    }

    wsSocket.onopen = () => {
        console.log('WebSocket connected');
        wsReconnectAttempts = 0;
        isWsConnected = true;
        updateConnectionStatus('connected');
    };

    wsSocket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleWebSocketMessage(data);
        } catch (e) {
            console.error('Error parsing WebSocket message:', e);
        }
    };

    wsSocket.onclose = (event) => {
        console.log('WebSocket disconnected:', event.code, event.reason);
        wsSocket = null;
        window.socket = null;
        isWsConnected = false;
        updateConnectionStatus('disconnected');
        scheduleWsReconnect();
    };

    wsSocket.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

function scheduleWsReconnect() {
    console.log(`attempting to reconnect to websocket..`);
    connectWebSocket();
}

function handlePromptProgress(prog) {
    let progressData = prog;
    try {
        if (typeof prog === 'string') {
            progressData = JSON.parse(prog);
        }
    } catch (e) {
        return;
    }

    typing.classList.toggle('show', false);
    typing.style.display = '';

    const cache = progressData.cache || 0;
    const processed = progressData.processed - cache;
    const total = progressData.total - cache;
    const percent = total > 0 ? Math.round((processed / total) * 100) : 0;
    const elapsed = progressData.time_ms / 1000;
    const remaining = (total - processed) > 0 ? (elapsed / processed) * (total - processed) : 0;

    // 1. create indicator
    if (!fancyProcessingIndicatorCreated && !catchingUpFromBuffer) {
        fancyProcessingIndicator = document.createElement('div');
        fancyProcessingIndicator.className = 'prompt-processing-indicator-wrapper tool-processing-content';

        const target = (typeof typing !== 'undefined' && typing) ? typing : chat;
        chat.insertBefore(fancyProcessingIndicator, target);

        fancyProcessingIndicator.innerHTML = `
        <div class="prompt-processing-indicator">
        <div class="progress-header">
        <span class="prompt-processing-percent">0%</span>
        <span class="prompt-processing-eta" style="opacity: 0.7">(ETA: 0s)</span>
        </div>
        <div class="prompt-progress-bar">
        <div class="prompt-progress-bar-fill" style="width: 0%"></div>
        </div>
        </div>
        `;

        progressBarFill = fancyProcessingIndicator.querySelector('.prompt-progress-bar-fill');
        progressTextPercent = fancyProcessingIndicator.querySelector('.prompt-processing-percent');
        progressTextETA = fancyProcessingIndicator.querySelector('.prompt-processing-eta');

        TypewriterAudioManager.playProcessingSound();
        scrollToBottom();

        fancyProcessingIndicatorCreated = true;
    }

    if (typeof toolProcessingIndicatorElement !== 'undefined' && toolProcessingIndicatorElement && toolProcessingIndicatorElement.updateProgress) {
        toolProcessingIndicatorElement.updateProgress(percent);
    }

    if (typeof progressBarFill !== 'undefined') {
        progressBarFill.style.width = `${percent}%`;
    }
    if (progressTextPercent && progressTextETA) {
        progressTextPercent.textContent = `${percent}%`;
        progressTextETA.textContent = `(ETA: ${Math.ceil(remaining)}s)`;
    }
}

/**
 * Unified token processor for both live streaming and initial buffer catch-up.
 * @param {Object} msg - The message object containing type and content (or tool_calls).
 * @param {boolean} isSimulated - If true, suppresses playback sounds (used for initial catch-up).
 */
function processToken(msg, isSimulated = false) {
    const type = msg.type || 'content';
    const content = msg.content || '';

    // 1. Handle Reasoning
    if (type === 'reasoning' && content) {
        clearProcessingIndicators();
        appendStreamText(type, content, false);
        renderStreamSegments(window._currentAiMsgDiv);
        if (!isSimulated && window._currentUseStreamingSound) {
            TypewriterAudioManager.play('token');
        }
        updateStopButtonState();
        return;
    }

    // 2. Handle Content
    if (type === 'content' && content) {
        clearProcessingIndicators();
        appendStreamText(type, content, window._currentUseTypewriter);

        if (window._currentUseTypewriter) {
            // Manually queue characters for typewriter mode
            if (typeof activeTypewriterSegId !== 'undefined' && activeTypewriterSegId !== -1) {
                const activeSeg = streamSegments.find(s => s.id === activeTypewriterSegId);
                if (activeSeg && activeSeg.type === 'content') {
                    for (const char of content) {
                        typewriterQueue.push({ segId: activeSeg.id, char });
                    }
                    if (typeof isTypewriterRunning === 'undefined' || !isTypewriterRunning) {
                        startTypewriterProcessSegments(window._currentAiMsgDiv);
                    }
                }
            }
        } else {
            renderStreamSegments(window._currentAiMsgDiv);
            if (!isSimulated && window._currentUseStreamingSound) {
                TypewriterAudioManager.play('token');
            }
        }
        updateStopButtonState();
        return;
    }

    // 3. Handle Tool Call Delta
    if (type === 'tool_call_delta') {
        clearProcessingIndicators();

        ensureToolCallsSegment();
        handleToolCallDelta(msg, window._currentAiMsgDiv, window._currentAiWrapper);
        if (!isSimulated && window._currentUseStreamingSound && !window._currentUseTypewriter) {
            TypewriterAudioManager.play('token');
        }
        updateStopButtonState();
        return;
    }

    // 4. Handle Completed Tool Calls
    if (type === 'tool_calls') {
        clearProcessingIndicators();

        finalizeStreamingToolCalls(msg.tool_calls || [], window._currentAiMsgDiv);
        TypewriterAudioManager.stopProcessingSound();
        updateStopButtonState();
        return;
    }

    // 5. Handle Tool Responses
    if (type === 'tool') {
        handleToolResponse(msg, window._currentAiMsgDiv);
        TypewriterAudioManager.playProcessingSound();
        updateStopButtonState();
        return;
    }
}

function handleWebSocketMessage(data) {
    // Handle typed messages from backend
    if (data.type === 'sync_state') {
        if (data.buffer.length > 0) {
            catchingUpFromBuffer = true;
            loadChat(data.active_chat_id, catchingUpFromBuffer);
            createAiWrapper();
            data.buffer.forEach(token => processToken(token, true));
            clearProcessingIndicators();
        } else {
            if (data.active_chat_id) {
                loadChat(data.active_chat_id);
            }
        }
        return;
    }

    if (data.type === 'chat_switched') {
        if (data.chat_id === currentChatId) return;
        window.loadChat(data.chat_id, catchingUpFromBuffer);
        return;
    }

    if (data.type === 'user_message_added') {
        handleNewMessage(data.message);
        return;
    }

    if (data.type === 'user_message_confirmed') {
        const msgWrapper = chat.querySelector(`[data-index="${data.index}"]`);
        if (msgWrapper) {
            msgWrapper.classList.remove('sending');
        }
        typing.classList.toggle('show', true);
        return;
    }

    if (data.type === 'token') {
        if (!isStreaming) {
            // keep input field blocked until the stream is done
            setInputState(true, false, true);
            isStreaming = true;
            isDataStreaming = true;
        }

        // Cleanup upload queue
        if (window.upload_queue) {
            window.upload_queue.wrappers.forEach(w => w.remove());
            window.upload_queue.files = [];
            window.upload_queue.wrappers = [];
            window.updateUploadQueueUI();
        }

        // Extract token type and content correctly
        let tokenType = 'content';
        let msgPayload = data.message;

        if (data.message) {
            tokenType = data.message.type || 'content';
        } else if (data.content) {
            msgPayload = { type: 'content', content: data.content };
            tokenType = 'content';
        }

        // Metadata/Control tokens
        if (tokenType === 'token_usage') {
            updateTokenUsage();
            return;
        }

        if (tokenType === 'prompt_progress') {
            handlePromptProgress(data.message ? data.message.content : data.content);
            return;
        }

        // Real-time token processing
        if (!window._currentAiMsgDiv) {
            createAiWrapper();
        } else if (window._currentAiWrapper && !window._currentAiWrapper.parentNode) {
            chat.insertBefore(window._currentAiWrapper, typing);
        }

        processToken(msgPayload, false);
        return;
    }

    if (data.type === 'stream_complete') {
        isDataStreaming = false;
        isStreaming = false;
        streamStarted = false;
        fancyProcessingIndicatorCreated = false;
        updateStopButtonState();

        if (!window._currentAiWrapper) {
            return;
        }

        window._currentAiWrapper.dataset.index = data.index;

        if (typeof isTypewriterRunning === 'undefined' || !isTypewriterRunning) {
            if (window._currentAiWrapper) {
                finalizeStreamingUI(window._currentAiWrapper, window._currentAiMsgDiv);
            }
        } else {
            waitForTypewriter().then(() => {
                if (window._currentAiWrapper) {
                    finalizeStreamingUI(window._currentAiWrapper, window._currentAiMsgDiv);
                }
            });
        }
        window._streamInitialized = false;
        return;
    }

    if (data.type === 'messages_updated') {
        try {
            renderAllMessages(data.messages, false);
        } catch (e) {
            console.log(e);
        }
        return;
    }

    if (data.type === 'push') {
        handleNewMessage(data.message);
        return;
    }

    if (data.type === 'chat_metadata_updated') {
        updateChatTitleBar(data.title, data.tags || []);
        loadChats();
        return;
    }

    if (data.type === 'status_updated') {
        updateConnectionStatus(data.status);
        return;
    }

    if (data.type === 'log') {
        handleLogMessage(data);
        return;
    }

    if (data.type === 'log_history') {
        handleLogHistory(data.logs);
        return;
    }

    if (data.type === 'ready') {
        // close the modal and resume everything
        closeModal('log');
    }
    if (data.type === 'shutdown') {
        // show system logs
        closeModal('settings');
        showModal('log', true);
    }

    if (data.type === 'error') {
        handleServerError(data.error);
        return;
    }
}

function handleNewMessage(msg) {
    if (!isWsConnected) return;
    if (!msg || msg.index === undefined) return;
    if (msg.index < lastMessageIndex) return;

    renderSingleMessage(msg, msg.index, true);
    lastMessageIndex = msg.index + 1;
    scrollToBottom();
    updateTokenUsage();
}
