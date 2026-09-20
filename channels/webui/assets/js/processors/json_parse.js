function partialJsonParse(str) {
    if (!str || !str.trim()) return {};

    try {
        return JSON.parse(str);
    } catch (e) {
        let completed = str.trim();
        completed = completed.replace(/,\s*([}\]])/g, '$1');

        let openBraces = (completed.match(/{/g) || []).length;
        let closeBraces = (completed.match(/}/g) || []).length;
        let openBrackets = (completed.match(/\[/g) || []).length;
        let closeBrackets = (completed.match(/]/g) || []).length;

        const openQuotes = (completed.match(/"(?<!\\)"/g) || []).length;
        if (openQuotes % 2 !== 0) {
            completed += '"';
        }

        while (closeBraces < openBraces) { completed += '}'; closeBraces++; }
        while (closeBrackets < openBrackets) { completed += ']'; closeBrackets++; }

        try {
            return JSON.parse(completed);
        } catch (e2) {
            return { _raw: formatRawString(str) };
        }
    }
}

/**
 * Converts JSON escape sequences in a raw (unparseable) string into their
 * actual characters so the webui can display them properly.
 *
 * The single-pass regex ensures `\\n` (escaped backslash + 'n') still
 * displays as the literal text `\n` instead of being treated as a newline.
 */
function formatRawString(str) {
    return str.replace(/\\(n|r|t|b|f|u[0-9a-fA-F]{4}|["'\/\\])/g, (match, esc) => {
        switch (esc[0]) {
            case 'n': return '\n';
            case 'r': return '\r';
            case 't': return '\t';
            case 'b': return '\b';
            case 'f': return '\f';
            case 'u': return String.fromCharCode(parseInt(esc.slice(1), 16));
            default: return esc[0]; // ", ', /, \
        }
    });
}
