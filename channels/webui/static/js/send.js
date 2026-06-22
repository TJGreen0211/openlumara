// =============================================================================
// Timing Statistics State
// =============================================================================
let currentTokensPerSecond = 0;
let promptProgress = null;
let totalPromptTokens = 0;
let totalGenTokens = 0;

// =============================================================================
// Main Send Function
// =============================================================================

async function send(providedContent = null) {
    const isRegenerate = providedContent !== null;
    const rawContent = providedContent !== null ? providedContent : inputField.value.trim();
    const message = typeof rawContent === 'string' ? rawContent : extractTextContent(rawContent);

    // block sending while a stream is ongoing
    if (isStreaming) return;

    // allow send if typewriter is running but streaming has finished.
    // just skip the typing animations
    // makes you also able to skip by just pressing enter
    if (isTypewriterRunning) { await stopGeneration(); }

    // reject blank messages
    if (!message && !isRegenerate) return;

    if (!isRegenerate) {
        clearInput();
    }

    promptProcessingReceived = false;
    typewriterQueue = [];
    displayedContent = '';
    isTypewriterRunning = false;
    resetStreamState();

    // Check API status
    let isConnected = false;
    try {
        const statusResponse = await fetch('/api/status', { signal: AbortSignal.timeout(5000) });
        if (statusResponse.ok) {
            const statusData = await statusResponse.json();
            if (statusData.connected) {
                isConnected = true;
            }
        }
    } catch (err) {
        console.error('Could not check API status:', err);
    }

    if (!isConnected) {
        const reconnected = await reconnectApi();
        if (!reconnected) {
            removePlaceholder();
            showApiConfigError('API is not connected. Cannot regenerate response.', 'connection_failed');
            return;
        }
        isConnected = true;
    }

    // Build payload
    const hasFiles = window.upload_queue && window.upload_queue.files.length > 0;
    const isMultimodalInput = typeof rawContent !== 'string';
    let payloadBody;

    if (!hasFiles && !isMultimodalInput) {
        payloadBody = { role: "user", content: rawContent };
    } else {
        let contentPayload = [];
        if (typeof rawContent === 'string') {
            contentPayload.push({ type: 'text', text: rawContent });
        } else {
            contentPayload = [...rawContent];
        }
        if (hasFiles) {
            const queuedContents = window.upload_queue.files.map(f => f.content);
            contentPayload.push(...queuedContents);
        }
        contentPayload = contentPayload.flat();
        payloadBody = { role: "user", content: contentPayload };
    }

    currentController = new AbortController();

    // Update stop button to show streaming indicator when tokens start
    updateStopButtonState();

    let playedCompletionSound = false;

    // Play send message sound
    TypewriterAudioManager.play('send_message');

    let streamHadError = false;
    let streamStarted = false;

    const typewriterEnabled = localStorage.getItem("typewriterEnabled") === 'true';
    const typewriterSpeed = parseInt(localStorage.getItem("typewriterSpeed") ?? "30", 10);
    const useTypewriter = typewriterEnabled && typewriterSpeed > 0;
    const useStreamingSound = localStorage.getItem("tokenEnabled") === 'true';

    let progressBarFill = null;
    let progressTextPercent = null;
    let progressTextETA = null;

    scrollToBottom();

    // Send via WebSocket
    if (window.socket && window.socket.readyState === WebSocket.OPEN) {
        window.socket.send(JSON.stringify({
            type: 'user_message',
            content: payloadBody
        }));
    } else {
        // Socket not open — queue the message for later delivery
        safeSocketSend({
            type: 'user_message',
            content: payloadBody
        });
        // Show a brief notification that the message is queued
        showApiStatusUpdate('info', 'Message queued — will send when reconnected.');
    }
}

async function sendCommand(message) {
    try {
        if (message.toLowerCase() === '/connect') {
            await reconnectApi();
            return;
        }

        if (message.startsWith("/stop") || message.startsWith("STOP")) {
            await stopGeneration(true);
        } else {
            // Create placeholder for the command being sent
            placeholderUserWrapper = createPlaceholderUserMessage(message);
            chat.insertBefore(placeholderUserWrapper, typing);
            scrollToBottom();

            const response = await fetch('/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({role: "user", content: message })
            });

            if (response.status === 503) {
                let errorData;
                try {
                    errorData = await response.json();
                } catch (e) {
                    errorData = { error: 'API is not available.' };
                }
                showApiConfigError(
                    errorData.error || 'API is not available.',
                    errorData.error_type,
                    errorData.action
                );
                removePlaceholder();
                return;
            }

            removePlaceholder();
        }

        const chatResponse = await fetch('/chat/current');
        const chatData = await chatResponse.json();
        if (chatData.success && chatData.chat) {
            currentChatId = chatData.chat.id;
            updateChatTitleBar(
                chatData.chat.title,
                chatData.chat.tags || []
            );
        }

        await loadChats();
    } catch (err) {
        console.error('Command failed:', err);
    }
}

function renderStats(container, tps, genTokens) {
    // Format tokens per second
    const tpsText = tps > 0 ? tps.toFixed(2) : "0.00";

    container.innerHTML = `
    <div class="stat-badge">
    <span class="stat-value">${tpsText} t/s</span>
    </div>
    `;
}

// =============================================================================
// Optimistic UI Helpers
// =============================================================================

function createAiWrapper() {
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

    aiWrapper.classList.remove('hidden');

    window._currentAiWrapper = aiWrapper;
    window._currentAiMsgDiv = aiMsgDiv;
    window._currentUseTypewriter = localStorage.getItem("typewriterEnabled") === 'true';
    window._currentUseStreamingSound = localStorage.getItem("tokenEnabled") === 'true';

    if (fancyProcessingIndicator) {
        fancyProcessingIndicator.remove();
        fancyProcessingIndicator = null;
    }

    return aiWrapper;
}

function createPlaceholderUserMessage(text) {
    const wrapper = document.createElement('div');
    wrapper.className = 'message-wrapper user user-placeholder animate-in';

    const msgDiv = document.createElement('div');
    msgDiv.className = 'message user';

    const contentContainer = document.createElement('div');
    contentContainer.className = 'message-content-container';
    contentContainer.textContent = text;

    const status = document.createElement('div');
    status.className = 'placeholder-status';
    status.textContent = 'Sending...';

    msgDiv.appendChild(contentContainer);
    msgDiv.appendChild(status);
    wrapper.appendChild(msgDiv);

    return wrapper;
}

function removePlaceholder() {
    if (placeholderUserWrapper) {
        placeholderUserWrapper.remove();
        placeholderUserWrapper = null;
    }
}

// =============================================================================
// Error Handlers
// =============================================================================
/**
 * Centralized dictionary to map technical errors to human-friendly
 * messages and actionable advice.
 */
const ERROR_MAP = {
    // API/Connection Errors
    'not_connected': {
        title: 'Connection Lost',
        message: 'We lost touch with the AI server.',
        action: 'Please check your API settings or connection.',
        icon: 'connection_lost'
    },
    'auth_failed': {
        title: 'Authentication Failed',
        message: 'Your API key is invalid or has expired.',
        action: 'Please check your API key in the settings.',
        icon: 'lock'
    },
    'rate_limit': {
        title: 'Too Many Requests',
        message: 'You are sending messages too quickly.',
        action: 'Please wait a moment before trying again.',
        icon: 'clock'
    },
    'api_error': {
        title: 'AI Service Error',
        message: 'The AI provider returned an error.',
        action: 'Try rephrasing your prompt or try again later.',
        icon: 'alert_circle'
    },
    'stream_failed': {
        title: 'Stream Interrupted',
        message: 'The response was cut off unexpectedly.',
        action: 'Try clicking "Regenerate" to restart.',
        icon: 'server'
    },
    'server_error': {
        title: 'Server Hiccup',
        message: 'The server encountered an internal error.',
        action: 'This is usually temporary. Please try again in a few seconds.',
        icon: 'server'
    },
    'network_error': {
        title: 'Network Error',
        message: 'Unable to reach the server.',
        action: 'Check your internet connection and try again.',
        icon: 'globe'
    },
    'websocket_not_open': {
        title: 'WebSocket Unavailable',
        message: 'The connection is not ready yet. Your message is queued.',
        action: 'It will be sent automatically when reconnected.',
        icon: 'server'
    },
    'default': {
        title: 'Something went wrong',
        message: 'An unexpected error occurred.',
        action: 'Please try again.',
        icon: 'error'
    }
};

// Helper to get icon SVG based on type
function getErrorIcon(type) {
    const icons = {
        'lock': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
        'clock': '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
        'alert_circle': '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
        'globe': '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
        'server': '<rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>'
    };
    return icons[type] || icons['alert_circle'];
}


/**
 * Extracts a user-friendly message from a raw error string.
 * Parses JSON error responses to extract the 'message' field if present.
 */
function extractErrorMessage(rawError) {
    if (!rawError) return null;
    
    // Try to parse as JSON to extract the message field
    try {
        // Handle "Error code: 402 - {...}" format
        const jsonMatch = rawError.match(/Error code: \d+ - (\{[\s\S]*\})/);
        const jsonStr = jsonMatch ? jsonMatch[1] : rawError;
        
        const parsed = JSON.parse(jsonStr);
        
        // Navigate common error structures
        if (parsed.error?.message) return parsed.error.message;
        if (parsed.message) return parsed.message;
        if (parsed.error?.error?.message) return parsed.error.error.message;
        if (typeof parsed.error === 'string') return parsed.error;
    } catch (e) {
        // Not JSON, check for common patterns
    }
    
    // Try to extract message from string format like "{'error': {'message': '...'}}"
    const messageMatch = rawError.match(/'message':\s*'([^']+)'/);
    if (messageMatch) return messageMatch[1];
    
    const messageMatch2 = rawError.match(/"message":\s*"([^"]+)"/);
    if (messageMatch2) return messageMatch2[1];
    
    return null;
}

/**
 * Handles HTTP error responses (4xx, 5xx)
 */
async function handleServerError(response, aiWrapper) {
    let errorType = 'server_error';
    let customMessage = '';
    let rawError = '';

    try {
        const errorData = await response.json();
        // Use the error_type provided by backend, or fallback to the error message
        errorType = errorData.error_type || errorData.error || 'server_error';
        customMessage = errorData.message || '';
        rawError = errorData.raw_error || '';
    } catch (e) {
        // Fallback if JSON parsing fails
        if (response.status === 401 || response.status === 403) errorType = 'auth_failed';
        else if (response.status === 429) errorType = 'rate_limit';
        else if (response.status >= 500) errorType = 'server_error';
    }

    const info = ERROR_MAP[errorType] || ERROR_MAP['default'];

    // Try to extract a meaningful message from raw error
    const extractedMessage = extractErrorMessage(rawError);
    
    // Use extracted message, then custom message, then generic message
    const displayMsg = extractedMessage || customMessage || info.message;

    // Pass both the display message and raw error
    showApiConfigError(displayMsg, errorType, info.action, rawError);
    removePlaceholder();

    if (aiWrapper && aiWrapper.parentNode) {
        aiWrapper.remove();
    }

    finishStream();
}

/**
 * Handles errors that occur mid-stream (sent via data: {"type": "error", ...})
 */
function handleInlineError(data, aiMsgDiv, aiWrapper, streamStarted) {
    if (!streamStarted) aiWrapper.classList.remove('hidden');

    const errorDetails = data.error_data || {};
    const type = errorDetails.error || 'api_error';
    const info = ERROR_MAP[type] || ERROR_MAP['default'];

    // Try to extract a meaningful message from raw error
    const rawError = errorDetails.raw_error || '';
    const extractedMessage = extractErrorMessage(rawError);
    
    // Use extracted message, then backend message, then generic message
    const userMessage = extractedMessage || errorDetails.message || info.message;

    // Build the error display - show message prominently, raw error in details
    let errorContent = escapeHtml(userMessage);
    if (rawError) {
        errorContent += `<details class="api-error-details"><summary>Technical details</summary><pre>${escapeHtml(rawError)}</pre></details>`;
    }

    aiMsgDiv.innerHTML = `
    <div class="api-error-inline">
    <div class="api-error-header">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    ${getErrorIcon(type)}
    </svg>
    <span class="api-error-title">${escapeHtml(info.title)}</span>
    </div>
    <div class="api-error-message">${errorContent}</div>
    <div class="api-error-footer">
    <div class="api-error-action">${escapeHtml(info.action)}</div>
    </div>
    </div>`;
}

/**
 * Handles hard network failures (DNS, CORS, Offline)
 */
function handleCatchError(err, aiMsgDiv, aiWrapper, streamStarted) {
    if (!streamStarted) aiWrapper.classList.remove('hidden');

    let type = 'network_error';
    // Detect if it's a specific browser error
    if (err.name === 'AbortError') return;

    const info = ERROR_MAP[type];

    aiMsgDiv.innerHTML = `
    <div class="api-error-inline">
    <div class="api-error-header">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    ${getErrorIcon(type)}
    </svg>
    <span class="api-error-title">${escapeHtml(info.title)}</span>
    </div>
    <div class="api-error-message">${escapeHtml(err.message)}</div>
    <div class="api-error-footer">
    <div class="api-error-action">${escapeHtml(info.action)}</div>
    </div>
    </div>`;
}
