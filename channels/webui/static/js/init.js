// =============================================================================
// WebSocket Connection Management (Module Level)
// =============================================================================

let wsSocket = null;

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
        // Don't close here - onclose will fire after onerror
    };
}

function scheduleWsReconnect() {
    console.log(`attempting to reconnect to websocket..`);
    setTimeout(connectWebSocket, 1000);
}

function handlePromptProgress(prog) {
    let progressData = prog;
    try {
        if (typeof prog === 'string') {
            progressData = JSON.parse(prog);
        }
    } catch (e) {
        console.error('[DEBUG] Failed to parse progress data:', e);
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

    // 1. Handle Creation if not exists
    if (!fancyProcessingIndicator) {
        console.log('[DEBUG] Creating prompt processing indicator');
        fancyProcessingIndicator = document.createElement('div');
        fancyProcessingIndicator.className = 'prompt-processing-indicator-wrapper tool-processing-content';

        // Ensure typing is available and we are in the chat
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

        // Cache DOM references
        progressBarFill = fancyProcessingIndicator.querySelector('.prompt-progress-bar-fill');
        progressTextPercent = fancyProcessingIndicator.querySelector('.prompt-processing-percent');
        progressTextETA = fancyProcessingIndicator.querySelector('.prompt-processing-eta');

        TypewriterAudioManager.playProcessingSound();
        scrollToBottom();
    }

    // 2. Update Tool Processing Indicator if it exists
    if (typeof toolProcessingIndicatorElement !== 'undefined' && toolProcessingIndicatorElement && toolProcessingIndicatorElement.updateProgress) {
        toolProcessingIndicatorElement.updateProgress(percent);
    }

    // 3. Update progress bar and text (Direct DOM manipulation)
    if (progressBarFill) {
        progressBarFill.style.width = `${percent}%`;
    }
    if (progressTextPercent && progressTextETA) {
        progressTextPercent.textContent = `${percent}%`;
        progressTextETA.textContent = `(ETA: ${Math.ceil(remaining)}s)`;
    }
}

function handleWebSocketMessage(data) {
    // Handle typed messages from backend
    if (data.type === 'sync_state') {
        // Sync handshake: restore active chat and buffer
        if (data.active_chat_id) {
            // We need to make sure the chat is fully loaded before syncing state
            // In a real app, this would be an async operation.
            // For now, we trigger the switch.
            window.switchChat(data.active_chat_id, true);
        }
        if (data.buffer) {
            // Append buffer to current message if streaming
            appendStreamText(data.buffer);
            if (window._currentAiMsgDiv) {
                renderStreamSegments(window._currentAiMsgDiv);
            }
        }
        return;
    }
    if (data.type === 'chat_switched') {
        // Force switch chat on all devices
        if (data.chat_id === currentChatId) {
            return;
        }

        console.log("[DEBUG] switching chats..");

        window.loadChat(data.chat_id);
        return;
    }
    if (data.type === 'user_message_added') {
        handleNewMessage(data.message);
        console.log(`[DEBUG] Adding new user messsage`);
        console.log(data.message);
        setInputState(true, false, true);
        return;
    }
    if (data.type === 'user_message_confirmed') {
        console.log(`[DEBUG] Got user message confirmation for ID ${data.index}`)
        // Remove 'sending...' status from the user message
        const msgWrapper = chat.querySelector(`[data-index="${data.index}"]`);
        if (msgWrapper) {
            console.log(`[DEBUG] Confirming user message index: ${data.index}`);
            msgWrapper.classList.remove('sending');
        }

        typing.classList.toggle('show', true);

        return;
    }

    if (data.type === 'token') {
        // empty the upload queue
        if (window.upload_queue) {
            window.upload_queue.wrappers.forEach(w => w.remove());
            window.upload_queue.files = [];
            window.upload_queue.wrappers = [];
            window.updateUploadQueueUI();
        }

        // Extract token type and content correctly
        let tokenType = 'content';
        let tokenContent = '';

        if (data.message) {
            tokenType = data.message.type || 'content';
            tokenContent = data.message.content || '';
        } else if (data.content) {
            tokenContent = data.content;
        }

        if (tokenType === 'token_usage') {
            updateTokenUsage();
            return;
        }

        // Handle prompt progress
        if (tokenType === 'prompt_progress') {
            console.log("handling prompt progress");
            handlePromptProgress(tokenContent);
            return;
        }

        // Real-time token broadcasting
        if (!window._currentAiMsgDiv) {
            // If no AI message wrapper exists, it means a new stream has started.
            // We create the streaming AI wrapper here.
            console.log('[DEBUG] First token received. Creating streaming AI wrapper.');
            
            const aiWrapper = document.createElement('div');
            aiWrapper.className = 'message-wrapper ai hidden streaming';

            const aiMsgDiv = document.createElement('div');
            aiMsgDiv.className = 'message ai';
            aiWrapper.appendChild(aiMsgDiv);

            const aiActions = createActionButtons('assistant', 'streaming', '', true);
            const statsDiv = document.createElement('div');
            statsDiv.id = 'message-stats-container';
            statsDiv.className = 'action-stats';
            const actionsRow = document.createElement('div');
            actionsRow.className = 'actions-stats-row';
            actionsRow.appendChild(aiActions);
            actionsRow.appendChild(statsDiv);
            aiWrapper.appendChild(actionsRow);
            
            // FIX: Make the wrapper visible immediately
            aiWrapper.classList.remove('hidden');
            
            // Set globals for subsequent tokens in this stream
            window._currentAiWrapper = aiWrapper;
            window._currentAiMsgDiv = aiMsgDiv;
            window._currentUseTypewriter = localStorage.getItem("typewriterEnabled") === 'true';
            window._currentUseStreamingSound = localStorage.getItem("tokenEnabled") === 'true';

            // Initialize local streaming state
            isStreaming = true;
            isDataStreaming = true;

            // remove typing indicator
            typing.classList.toggle('show', false);
            if (typing) { typing.style.display = ''; }

            // Remove progress indicator when first token arrives
            if (fancyProcessingIndicator) {
                fancyProcessingIndicator.remove();
                fancyProcessingIndicator = null;
            }
        } else if (window._currentAiWrapper && !window._currentAiWrapper.parentNode) {
            // Fallback: Insert AI wrapper if it was created but not yet in the DOM
            chat.insertBefore(window._currentAiWrapper, typing);
        }

        if (tokenType === 'reasoning' && tokenContent) {
            appendStreamText(tokenType, tokenContent, false);
            renderStreamSegments(window._currentAiMsgDiv);
            if (window._currentUseStreamingSound) {
                TypewriterAudioManager.play('token');
            }
            updateStopButtonState();
        } else if (tokenType === 'content' && tokenContent) {
            appendStreamText(tokenType, tokenContent, window._currentUseTypewriter);
            if (window._currentUseTypewriter) {
                // Manually queue characters for typewriter mode
                if (typeof activeTypewriterSegId !== 'undefined' && activeTypewriterSegId !== -1) {
                    const activeSeg = streamSegments.find(s => s.id === activeTypewriterSegId);
                    if (activeSeg && activeSeg.type === 'content') {
                        for (const char of tokenContent) {
                            typewriterQueue.push({ segId: activeSeg.id, char });
                        }
                        if (typeof isTypewriterRunning === 'undefined' || !isTypewriterRunning) {
                            startTypewriterProcessSegments(window._currentAiMsgDiv);
                        }
                    }
                }
            } else {
                renderStreamSegments(window._currentAiMsgDiv);
                if (window._currentUseStreamingSound) {
                    TypewriterAudioManager.play('token');
                }
            }
            updateStopButtonState();
        } else if (tokenType === 'tool_call_delta') {
            // Handle tool call deltas
            ensureToolCallsSegment();

            handleToolCallDelta(data.message, window._currentAiMsgDiv, window._currentAiWrapper);

            if (window._currentUseStreamingSound && !window._currentUseTypewriter) {
                TypewriterAudioManager.play('token');
            }
            updateStopButtonState();
        } else if (tokenType === 'tool_calls') {
            // Handle completed tool calls
            finalizeStreamingToolCalls(data.message.tool_calls || [], window._currentAiMsgDiv);
            TypewriterAudioManager.stopProcessingSound();
            updateStopButtonState();
        } else if (tokenType === 'tool') {
            // Handle tool responses
            handleToolResponse(data.message, window._currentAiMsgDiv);
            TypewriterAudioManager.playProcessingSound();
            updateStopButtonState();
        }
        return;
    }
    if (data.type === 'stream_complete') {
        // Signal end of streaming
        isDataStreaming = false; // Mark stream as complete
        isStreaming = false; // Reset global flag
        updateStopButtonState(); // Update button state immediately

        window._currentAiWrapper.dataset.index = data.index;
        
        // Wait for typewriter to finish if it's still running
        if (typeof isTypewriterRunning === 'undefined' || !isTypewriterRunning) {
            if (window._currentAiWrapper) {
                finalizeStreamingUI(window._currentAiWrapper, window._currentAiMsgDiv);
            }
        } else {
            // If typewriter is running, wait for it to finish before finalizing
            waitForTypewriter().then(() => {
                if (window._currentAiWrapper) {
                    finalizeStreamingUI(window._currentAiWrapper, window._currentAiMsgDiv);
                }
            });
        }
        window._streamInitialized = false;
        return;
    }
    if (data.type === 'push') {
        console.log("[DEBUG] handling push message");
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
    if (data.type === 'error') {
        handleServerError(data.error);
        return;
    }
}

function handleNewMessage(msg) {
    // Only process if we have a valid WebSocket connection
    if (!isWsConnected) return;
    console.log("checking if message index..");
    console.log(msg);
    if (!msg || msg.index === undefined) return;
    console.log("check proceeded");
    
    // Validate index is sequential (not older than what we already have)
    if (msg.index < lastMessageIndex) {
        console.log('Skipping old message, index:', msg.index, 'current:', lastMessageIndex);
        return;
    }

    renderSingleMessage(msg, msg.index, true);
    // Update lastMessageIndex to be one past the last rendered message
    lastMessageIndex = msg.index + 1;
    scrollToBottom();
    updateTokenUsage();
}

// =============================================================================
// Initialization
// =============================================================================

async function init() {
    try {
        requestNotificationPermission();
        document.addEventListener('click', () => {
            if (typeof notificationPermission !== 'undefined' && notificationPermission === 'default') {
                requestNotificationPermission();
            }
        }, { once: true });

        await checkConnection();
        if (isConnected) {
            await restoreCurrentChat();
        }
    } catch (err) {
        console.error('Failed to initialize connection:', err);
        isConnected = false;
        updateConnectionStatus('disconnected');
        scheduleReconnect();
    }

    try {
        const savedFontSize = localStorage.getItem('fontSize');
        if (savedFontSize) {
            document.documentElement.style.setProperty('--font-size-base', `${savedFontSize}px`);
        }

        loadTheme();
        loadChats();
        initTagFilterState();

        window.addEventListener('resize', handleTitleBarResize);

        // ─────────────────────────────────────────────────────────────
        // Safe Sound Default Initialization
        // ─────────────────────────────────────────────────────────────
        Object.entries(SOUND_DEFAULTS).forEach(([id, enabled]) => {
            const key = `${id}Enabled`;
            try {
                if (typeof localStorage !== 'undefined') {
                    const current = localStorage.getItem(key);
                    if (current === null) {
                        localStorage.setItem(key, String(enabled));
                    }
                }
            } catch (e) {
                console.warn('[Init] Storage unavailable, using runtime defaults');
            }
        });

        // ─────────────────────────────────────────────────────────────
        // WebSocket Connection
        // ─────────────────────────────────────────────────────────────
        connectWebSocket();

        // API status polling (this is still needed for API health)
        apiStatusIntervalId = setInterval(() => {
            if (isConnected) {
                checkApiStatus();
            }
        }, CONFIG.API_STATUS_INTERVAL);
    } catch (err) {
        console.error('Failed to initialize UI and polling:', err);
    }
}

// =============================================================================
// Log Modal Functions
// =============================================================================

let logAutoScroll = true;

function handleLogMessage(data) {
    const logContent = document.getElementById('log-log-content');
    if (!logContent) return;

    const timestamp = new Date().toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `<span class="log-timestamp">[${timestamp}]</span> <span class="log-category">[${data.category.toUpperCase()}]</span> <span class="log-message">${escapeHtml(data.message)}</span>`;

    logContent.appendChild(entry);

    if (logAutoScroll) {
        const logContainer = document.getElementById('log-log-container');
        if (logContainer) {
            logContainer.scrollTop = logContainer.scrollHeight;
        }
    }
}

function handleLogHistory(logs) {
    const logContent = document.getElementById('log-log-content');
    if (!logContent) return;

    // Clear existing logs
    logContent.innerHTML = '';

    // Add all historical logs
    for (const log of logs) {
        const entry = document.createElement('div');
        entry.className = 'log-entry';
        entry.innerHTML = `<span class="log-timestamp">[${new Date().toLocaleTimeString()}]</span> <span class="log-category">[${log.category.toUpperCase()}]</span> <span class="log-message">${escapeHtml(log.message)}</span>`;
        logContent.appendChild(entry);
    }
}

function clearLog() {
    const logContent = document.getElementById('log-log-content');
    if (logContent) {
        logContent.innerHTML = '';
    }
}

function toggleLogAutoScroll() {
    logAutoScroll = !logAutoScroll;
    const btn = document.getElementById('log-autoscroll-btn');
    if (btn) {
        btn.textContent = `Auto-scroll: ${logAutoScroll ? 'ON' : 'OFF'}`;
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

init();
