// =============================================================================
// Message Actions
// =============================================================================

async function editMessage(index, currentContent) {
    if (editingIndex !== null) {
        cancelEdit();
    }

    editingIndex = index;

    const messageEl = chat.querySelector(`[data-index="${index}"]`);
    if (!messageEl) return;

    const messageBubble = messageEl.querySelector('.message');
    let initialWidth = '100%';
    let initialHeight = 'auto';
    if (messageBubble) {
        const computedStyle = window.getComputedStyle(messageBubble);
        initialWidth = computedStyle.width;
        // Use the rendered height, but ensure it's at least 80px for usability
        const renderedHeight = Math.max(parseInt(computedStyle.height) || 80, 80);
        initialHeight = renderedHeight + 'px';
    }

    const editContainer = document.createElement('div');
    editContainer.className = 'edit-container';

    const textarea = document.createElement('textarea');
    textarea.className = 'edit-textarea';
    textarea.value = currentContent;
    textarea.setAttribute('aria-label', 'Edit message');
    textarea.style.width = initialWidth;
    textarea.style.height = initialHeight;

    const actions = document.createElement('div');
    actions.className = 'edit-actions';

    const saveBtn = document.createElement('button');
    saveBtn.className = 'edit-save';
    saveBtn.textContent = 'Save';
    saveBtn.onclick = () => saveEdit(index, textarea.value);

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'edit-cancel';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = cancelEdit;

    actions.appendChild(cancelBtn);
    actions.appendChild(saveBtn);
    editContainer.appendChild(textarea);
    editContainer.appendChild(actions);

    messageEl.innerHTML = '';
    messageEl.appendChild(editContainer);

    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);

    textarea.onkeydown = (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            saveEdit(index, textarea.value);
        }
        if (e.key === 'Escape') {
            cancelEdit();
        }
    };
}

async function saveEdit(index, newContent) {
    newContent = (newContent || '').trim();
    if (!newContent) {
        cancelEdit();
        return;
    }

    try {
        const response = await fetch('/edit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: index, content: newContent })
        });

        if (response.ok) {
            await syncMessages();
        }
    } catch (err) {
        console.error('Failed to edit message:', err);
    }

    editingIndex = null;

    // auto-regenerate from this point
    // await regenerateMessage(index);
}

async function cancelEdit() {
    editingIndex = null;
    await syncMessages();
}

async function deleteMessage(index) {
    if (!confirm('Delete this message and all messages after it?')) return;

    try {
        const response = await fetch('/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: index })
        });

        if (response.ok) {
            await syncMessages();
        }
    } catch (err) {
        console.error('Failed to delete message:', err);
    }
}

async function regenerateMessage(targetIndex) {
    // Validate index
    if (typeof targetIndex !== 'number' || targetIndex < 0) {
        console.error('Invalid index for regeneration:', targetIndex, typeof targetIndex);
        return;
    }

    if (isStreaming) {
        console.log('Cannot regenerate while streaming');
        return;
    }

    try {
        window.socket.send(JSON.stringify({
            type: 'message_regenerate',
            index: targetIndex
        }));
    } catch (err) {
        console.error('Failed to regenerate message:', err);
        showApiConfigError('Failed to regenerate message. Please try again.', 'connection_failed');
    }
}


