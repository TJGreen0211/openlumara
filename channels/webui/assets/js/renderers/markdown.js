// create a temporary div that gets used to syntax highlight
const _tempHighlightDiv = document.createElement('div');

// rendered html by source text. parse + sanitize + highlight is the slowest
// part of showing a chat, and a reload or chat switch re-renders the same
// messages over and over. a Map keeps insertion order, so the first key is
// the least recently used one
const _markdownCache = new Map();
const MARKDOWN_CACHE_SIZE = 500;

function renderMarkdown(text, cache = true) {
    if (!text) return '';

    if (cache) {
        const cached = _markdownCache.get(text);
        if (cached !== undefined) {
            // bump it to most recently used
            _markdownCache.delete(text);
            _markdownCache.set(text, cached);
            return cached;
        }
    }

    // parse the markdown to HTML
    let html = marked.parse(text);

    // protect against XSS
    html = DOMPurify.sanitize(html);

    // syntax highlighting
    if (typeof hljs !== 'undefined') {
        _tempHighlightDiv.innerHTML = html;

        _tempHighlightDiv.querySelectorAll('pre code').forEach((block) => {
            const lang = block.className.replace('language-', '') || undefined;
            hljs.highlightElement(block);
        });

        html = _tempHighlightDiv.innerHTML;
    }

    // add the copy button to all pre statements using a custom alpine directive
    // (defined in directives/copy-code.js)
    html = html.replace(/<pre><code/g, '<pre><code x-copy-code');

    if (cache) {
        _markdownCache.set(text, html);
        if (_markdownCache.size > MARKDOWN_CACHE_SIZE) {
            _markdownCache.delete(_markdownCache.keys().next().value);
        }
    }

    return html;
}
