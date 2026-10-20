/*
 * plain-text copies from the chat window.
 *
 * the dark theme paints message/code backgrounds through CSS custom
 * properties, and the browser's default copy also puts a text/html flavor on
 * the clipboard (firefox inlines the computed background-color into the
 * fragment). pasting into word/outlook/teams then drags the black background
 * along with the text. when a copy originates inside the message list,
 * rewrite the clipboard to text/plain only.
 */
function registerPlainCopy() {
    document.addEventListener('copy', (e) => {
        const messages = document.getElementById('messages');
        if (!messages) {
            return;
        }

        const selection = window.getSelection();
        if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
            return;
        }

        // only copies that originate inside the chat message list: sidebar,
        // settings modals and the input keep their default copy behavior
        if (!messages.contains(selection.getRangeAt(0).commonAncestorContainer)) {
            return;
        }

        const text = selection.toString();
        if (!text) {
            return;
        }

        e.clipboardData.clearData();
        e.clipboardData.setData('text/plain', text);
        e.preventDefault();
    });
}
