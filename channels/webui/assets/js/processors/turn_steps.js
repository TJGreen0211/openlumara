/*
 * splits an assistant turn's messages into display segments, so a run of
 * reasoning and tool calls collapses into one "steps" group instead of a
 * stack of separate blocks. content (the actual answer) breaks a run.
 *
 * works on both shapes a turn can have: streamed segments (with a `type`)
 * and history messages (raw openAI messages, no `type`)
 *
 * returns [{kind: 'steps', items: [{kind: 'reasoning'|'tools', message}], live}
 *          | {kind: 'content', message}]
 */
function groupTurnSteps(messages, streaming = false) {
    const segments = [];
    const last = messages?.at(-1);
    let steps = null;

    const pushStep = (kind, message) => {
        if (!steps) {
            steps = { kind: 'steps', items: [], live: false };
            segments.push(steps);
        }
        steps.items.push({ kind, message });
    };

    for (const message of messages || []) {
        const live = streaming && message === last;
        let reasoning, content, tools;

        if (message.type) {
            reasoning = message.type === 'reasoning';
            content = message.type === 'content';
            tools = message.type === 'tool_calls' || message.type === 'tool_call_delta';
        } else {
            reasoning = Object.hasOwn(message, 'reasoning_content');
            content = message.role === 'assistant' && typeof message.content === 'string';
            tools = Object.hasOwn(message, 'tool_calls');
        }

        // skip empty thoughts, but keep a live one so "thinking" shows right away
        if (reasoning && (live || message.reasoning_content?.trim())) {
            pushStep('reasoning', message);
        }

        // empty content (e.g. a history message that only carries tool calls) is skipped
        if (content && (live || message.content?.trim())) {
            steps = null;
            segments.push({ kind: 'content', message });
        }

        if (tools && (live || message.tool_calls?.length)) {
            pushStep('tools', message);
        }
    }

    // only the trailing group of a streaming turn can still be working
    if (streaming && segments.at(-1)?.kind === 'steps') {
        segments.at(-1).live = true;
    }

    return segments;
}

// one-line summary for a collapsed steps group, e.g. "Thought · Used 2 tools"
function stepsSummary(items) {
    const tools = items.flatMap(item => item.kind === 'tools' ? (item.message.tool_calls || []) : []);
    const parts = [];

    if (items.some(item => item.kind === 'reasoning')) { parts.push('Thought'); }
    if (tools.length) { parts.push(`Used ${tools.length} tool${tools.length === 1 ? '' : 's'}`); }

    return {
        label: parts.join(' · ') || 'Steps',
        tools: [...new Set(tools.map(tool => tool.function?.name).filter(Boolean))].join(', ')
    };
}

// what a live steps group is doing right now, e.g. "Thinking" or "Running web_search"
function stepsActivity(items, state) {
    const current = items.at(-1);

    if (current?.kind === 'reasoning' && state === 'thinking') { return 'Thinking'; }

    const pending = (current?.message.tool_calls || []).find(tool => !tool.response);
    if (pending?.function?.name) { return `Running ${pending.function.name}`; }

    return 'Working';
}
