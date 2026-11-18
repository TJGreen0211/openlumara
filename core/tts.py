"""
text-to-speech for openlumara

Replies are read aloud by an external TTS server with an OpenAI-style
/v1/audio/speech endpoint that streams raw PCM (tj-server-tools' Qwen3-TTS
service). The WebUI asks for a "plan" first - the reply cleaned for speech and
split into runs of one language - then streams each run's audio and plays it as
it arrives, so playback starts after the first chunk is generated.
"""
import re
import unicodedata

import anyio
import httpx

import core
import core.config

# httpx builds a new SSL context (loading the whole CA bundle) for every client it
# creates, which costs 0.5 s or more per request even for plain http. voice input sends
# a request about once a second, so the context is built once and shared.
_ssl_ctx = None


def _ssl_context():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = httpx.create_ssl_context()
    return _ssl_ctx


_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)


class TTSError(Exception):
    """Raised when text-to-speech isn't configured or the server fails"""


# languages the Qwen3-TTS model speaks
SUPPORTED_LANGUAGES = {"zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"}

# one request per run of same-language paragraphs, at most this long. the server
# splits a request into sentence groups itself and streams them back to back, so
# pieces only exist to switch languages (and to keep a single request bounded)
MAX_PIECE_CHARS = 4000

OMITTED = {
    "en": {"code": "Code omitted.", "table": "Table omitted."},
    "es": {"code": "Código omitido.", "table": "Tabla omitida."},
}


def _ch_cfg(key: str, default=None):
    """webui channel settings are global, not per-user"""
    return core.config.get("channels", "settings", "webui", key, default=default)


def server_url() -> str:
    return str(_ch_cfg("tts_server_url", default="") or "").strip()


def voice() -> str:
    return str(core.config.get("api", "voice_output_voice", default="default") or "default").strip()


def speed() -> float:
    """How fast replies are read: 1 is the voice's own pace. The server changes the pace without
    changing the pitch; it accepts 0.25-4 (the OpenAI range)."""
    try:
        value = float(core.config.get("api", "voice_output_speed", default=1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return min(4.0, max(0.25, value)) if value == value else 1.0


def default_language() -> str:
    """The user's voice language when the TTS model speaks it, else English."""
    lang = str(core.config.get("api", "voice_language", default="en") or "en").strip().lower()
    return lang if lang in SUPPORTED_LANGUAGES else "en"


# ---------------------------------------------------------------------------
# language: replies here are English or Spanish, so tell those two apart; any
# other configured language is used as-is
# ---------------------------------------------------------------------------

_EN_WORDS = set("the and is are was were to of that this you it for with on have has be not what which "
                "will would can could should there their they them your my we our from at by an "
                "in about also back so if or but all do does did how why who when where into out "
                "just than then these those its it's i'm i'll we'll you're don't".split())
_ES_WORDS = set("el la los las que de del y es son está están en por para con una un lo se su sus no "
                "como pero más muy también esto esta este hay ser fue era cuando donde porque".split())
_ES_CHARS = set("ñáéíóúü¿¡")


def detect_language(text: str, fallback: str) -> str:
    """'en' or 'es' from the words used; `fallback` when the text doesn't say clearly."""
    if fallback not in ("en", "es"):
        return fallback
    words = re.findall(r"[a-záéíóúüñ]+", text.lower())
    en = sum(w in _EN_WORDS for w in words)
    es = sum(w in _ES_WORDS for w in words) + 2 * sum(c in _ES_CHARS for c in text.lower())
    if es >= en + 2:
        return "es"
    if en >= es + 2:
        return "en"
    return fallback


# ---------------------------------------------------------------------------
# cleaning: replies are markdown, which shouldn't be read out symbol by symbol
# ---------------------------------------------------------------------------

_CODE_BLOCK = re.compile(r"```.*?(?:```|$)|~~~.*?(?:~~~|$)", re.S)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def _strip_symbols(text: str) -> str:
    # emoji and other pictographs
    return "".join(c for c in text if unicodedata.category(c) not in ("So", "Cs", "Co"))


def clean_for_speech(markdown: str, language: str = "en") -> str:
    """Markdown reply -> plain text to read aloud. Paragraphs stay separated by blank lines."""
    omitted = OMITTED.get(language, OMITTED["en"])
    text = _THINK.sub(" ", markdown or "")
    text = _CODE_BLOCK.sub(f"\n\n{omitted['code']}\n\n", text)

    lines, in_table = [], False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|"):
            if not in_table:
                lines.append(f"\n{omitted['table']}\n")
            in_table = True
            continue
        in_table = False
        lines.append(line)
    text = "\n".join(lines)

    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)              # images -> alt text
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)                # links -> link text
    text = re.sub(r"https?://\S+", "", text)                             # bare urls
    text = re.sub(r"<[^>]+>", " ", text)                                 # html tags
    text = re.sub(r"`([^`]*)`", r"\1", text)                             # inline code
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)                    # headings
    text = re.sub(r"(?m)^\s*>\s?", "", text)                             # quotes
    text = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", text)             # list markers
    text = re.sub(r"(?m)^\s*(?:[-*_]\s*){3,}$", "", text)                # horizontal rules
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)                      # bold
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)  # italic *x*
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)     # italic _x_
    text = re.sub(r"~~(.+?)~~", r"\1", text)                             # strikethrough
    text = _strip_symbols(text)

    # tidy: single spaces, at most one blank line between paragraphs
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# planning: split into pieces small enough to start playing quickly
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?…:;])\s+")


def _sentences(paragraph: str) -> list:
    # a line break inside a paragraph (e.g. former list items) also ends a sentence
    out = []
    for line in paragraph.split("\n"):
        out += [s.strip() for s in _SENTENCE_END.split(line) if s.strip()]
    return out


def _bounded(paragraph: str) -> list:
    """A paragraph as one string, or several (split between sentences) if it's very long."""
    parts, current = [], ""
    for sentence in _sentences(paragraph):
        if current and len(current) + 1 + len(sentence) > MAX_PIECE_CHARS:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def plan(markdown: str, fallback_language: str = None) -> list:
    """Splits a reply into pieces to stream in order: [{"text", "language"}], one per run of
    paragraphs in the same language. Paragraphs within a piece stay separated by blank lines."""
    fallback = fallback_language or default_language()
    reply_language = detect_language(markdown or "", fallback)
    text = clean_for_speech(markdown, reply_language)

    pieces = []
    for paragraph in text.split("\n\n"):
        for part in _bounded(paragraph):
            if not re.search(r"\w", part):
                continue   # nothing to say (e.g. only punctuation left after cleaning)
            lang = detect_language(part, reply_language)
            last = pieces[-1] if pieces else None
            if last and last["language"] == lang and len(last["text"]) + 2 + len(part) <= MAX_PIECE_CHARS:
                last["text"] += "\n\n" + part
            else:
                pieces.append({"text": part, "language": lang})
    return pieces


# ---------------------------------------------------------------------------
# synthesis
# ---------------------------------------------------------------------------

async def open_speech(text: str, language: str):
    """Starts streaming speech for `text`. Returns (sample_rate, chunks): chunks is an async
    iterator of raw 16-bit little-endian mono PCM, yielded as the server generates it.

    Errors (not configured, unreachable, rejected) raise TTSError here, before any audio,
    so callers can report them properly. Iterating to the end, or stopping early, closes
    the connection (which frees the TTS server's GPU for the next request)."""
    url = server_url()
    if not url:
        raise TTSError("Text-to-speech is not configured. Set tts_server_url in Settings -> Channels -> webui.")
    if language not in SUPPORTED_LANGUAGES:
        language = default_language()

    payload = {"input": text, "language": language, "voice": voice(), "response_format": "pcm"}
    if speed() != 1.0:
        payload["speed"] = speed()
    client = httpx.AsyncClient(verify=_ssl_context(), timeout=_REQUEST_TIMEOUT)
    try:
        resp = await client.send(client.build_request("POST", url, json=payload), stream=True)
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            raise TTSError(f"TTS server error {resp.status_code}: {body[:200]}")
        if "json" in resp.headers.get("content-type", ""):
            await resp.aclose()
            raise TTSError("The TTS server did not stream audio; does it support response_format=pcm?")
        sample_rate = int(resp.headers.get("x-sample-rate") or 24000)
    except httpx.HTTPError as e:
        await client.aclose()
        raise TTSError(f"Could not reach the TTS server ({url}): {e}")
    except BaseException:
        await client.aclose()
        raise

    async def chunks():
        try:
            async for data in resp.aiter_raw():
                if data:
                    yield data
        except httpx.HTTPError as e:
            core.log("tts", f"speech stream from {url} broke off: {e}")
        finally:
            # when the browser stops listening, the web server cancels this stream; the
            # close must still go through (shielded from that cancellation), or the TTS
            # server never sees the hang-up and keeps its GPU busy with unwanted audio
            with anyio.CancelScope(shield=True):
                await resp.aclose()
                await client.aclose()

    return sample_rate, chunks()
