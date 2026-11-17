"""
speech-to-text for openlumara

Two engines, both remote:
- server: a speech server (stt_whisper_server_url, or the per-user Voice URL) that takes
  multipart audio: tj-server-tools (Parakeet), a whisper.cpp server, or any
  OpenAI-style /audio/transcriptions endpoint
- openai: an OpenAI-compatible /audio/transcriptions endpoint on the main api url, used
  when no speech server is configured
"""
import json
import re

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


class STTError(Exception):
    """Raised when no engine is available or transcription fails"""


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------

def _log(msg: str):
    core.log("stt", msg)


def _ch_cfg(key: str, default=None):
    """webui channel settings are global, not per-user"""
    return core.config.get("channels", "settings", "webui", key, default=default)


def _server_url() -> str:
    """Whisper server endpoint (a full URL, e.g. http://host:5002/inference).

    The global channel setting (Settings -> Channels -> webui) wins over the
    per-user Voice URL (Settings -> Api)."""
    return (str(_ch_cfg("stt_whisper_server_url", default="") or "").strip()
            or str(core.config.get("api", "voice_url", default="") or "").strip())


# Non-speech artifacts the engines can emit (whisper.cpp special tokens,
# OpenAI-style markers, timestamps). Real dictation never contains these,
# so anything matching is pure noise that must not reach the input field.
_ARTIFACT_RE = re.compile(
    r"\[(?:BLANK_AUDIO|MUSIC|SILENCE|NOTSPEECH|SPEAKER\s*\d+|LANGUAGE\s+\w+"
    r"|\s*\d{1,2}(?::\d{2}(?::\d{2})?|\.\d{1,3})(?:[.,]\d{1,3})?"
    r"(?:\s*(?:-->|->|–|-|,)\s*\d{1,2}(?::\d{2}(?::\d{2})?|\.\d{1,3})(?:[.,]\d{1,3})?)?\s*"
    r")\]"
    r"|<\|[^|]*\|>",
    re.IGNORECASE,
)


# a sentence opening with an inverted mark glued to the one before ("pendientes.¿Quién")
_GLUED_OPENER_RE = re.compile(r"([.!?…:;,])([¿¡])")


def clean_transcription(text: str) -> str:
    """Strips non-speech artifacts ([Music], [BLANK_AUDIO], <|...|>, timestamps) from engine
    output, and puts back the space some engines drop before ¿ / ¡"""
    if not text:
        return ""
    cleaned = _ARTIFACT_RE.sub(" ", text)
    cleaned = _GLUED_OPENER_RE.sub(r"\1 \2", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def parse_transcription(resp):
    """Parses a transcription response into text.

    Handles OpenAI-style JSON, whisper.cpp /inference_json (text field)
    and whisper.cpp /inference (plain text).
    """
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("text", "transcription", "result"):
                if isinstance(data.get(key), str):
                    return data[key].strip()
    except (json.JSONDecodeError, ValueError):
        pass
    return (resp.text or "").strip()


def _api_key() -> str:
    key = core.config.get("api", "key", default="") or ""
    if not key or key == "openlumara-dummy-key":
        return ""
    return key


# ---------------------------------------------------------------------------
# server engine (whisper.cpp server)
# ---------------------------------------------------------------------------

async def transcribe_server(audio_data: bytes, url: str = None, language: str = "auto",
                            prompt: str = None, audio_format: str = "wav") -> str:
    url = url or _server_url()
    if not url:
        raise STTError(
            "No whisper server configured. Set the Voice URL in Settings -> Api "
            "or stt_whisper_server_url in Settings -> Channels -> webui."
        )

    data = {"response_format": "text"}
    if language and language.lower() != "auto":
        data["language"] = language
    if prompt:
        data["initial_prompt"] = prompt

    headers = {}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient(verify=_ssl_context(), timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                url,
                files={"file": (f"audio.{audio_format}", audio_data, f"audio/{audio_format}")},
                data=data,
                headers=headers
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise STTError(f"Whisper server error {e.response.status_code}: {e.response.text[:200]}")
    except httpx.HTTPError as e:
        raise STTError(f"Could not reach the whisper server ({url}): {e}")
    return parse_transcription(resp)


# ---------------------------------------------------------------------------
# openai engine (OpenAI-compatible /audio/transcriptions)
# ---------------------------------------------------------------------------

async def transcribe_openai(audio_data: bytes, language: str = "auto",
                            prompt: str = None, audio_format: str = "wav") -> str:
    api_url = (core.config.get("api", "url", default="") or "").strip()
    if not api_url or api_url == "http://API_URL_HERE/v1":
        raise STTError(
            "Voice input is not configured. Set api.url in Settings -> Api, "
            "or configure stt_whisper_server_url in Settings -> Channels -> webui."
        )

    data = {"model": "whisper-1", "response_format": "json"}
    if language and language.lower() != "auto":
        data["language"] = language
    if prompt:
        data["prompt"] = prompt

    headers = {}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient(verify=_ssl_context(), timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                api_url.rstrip("/") + "/audio/transcriptions",
                files={"file": (f"audio.{audio_format}", audio_data, f"audio/{audio_format}")},
                data=data,
                headers=headers
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise STTError(f"Voice API error {e.response.status_code}: {e.response.text[:200]}")
    except httpx.HTTPError as e:
        raise STTError(f"Could not reach the voice API ({api_url}): {e}")
    return parse_transcription(resp)


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

async def transcribe(audio_data: bytes, language: str = "auto",
                     prompt: str = None, audio_format: str = "wav") -> str:
    """Transcribes audio with the configured speech server, or the main API's
    /audio/transcriptions endpoint when no speech server is set."""
    url = _server_url()
    if url:
        text = await transcribe_server(audio_data, url, language, prompt, audio_format)
    else:
        text = await transcribe_openai(audio_data, language, prompt, audio_format)
    return clean_transcription(text)


# ---------------------------------------------------------------------------
# meetings (whole recording, with speaker labels)
# ---------------------------------------------------------------------------

async def transcribe_meeting(parts: list) -> list:
    """Sends a full meeting recording to the speech server with diarize=true.

    parts: [(filename, bytes, content_type)], one per recorder session (a paused
    meeting has several); the server joins them in order. Returns the server's
    segments: [{"start", "end", "speaker", "text"}], times in seconds.
    """
    url = _server_url()
    if not url:
        raise STTError("Meeting diarization needs stt_whisper_server_url set in Settings -> Channels -> webui.")

    headers = {}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # the server takes roughly 2 minutes per hour of audio, so allow long meetings
    timeout = httpx.Timeout(connect=5.0, read=1800.0, write=300.0, pool=5.0)
    try:
        async with httpx.AsyncClient(verify=_ssl_context(), timeout=timeout) as client:
            resp = await client.post(
                url,
                files=[("file", part) for part in parts],
                data={"diarize": "true", "response_format": "verbose_json"},
                headers=headers
            )
            resp.raise_for_status()
            segments = resp.json()["segments"]
    except httpx.HTTPStatusError as e:
        raise STTError(f"Speech server error {e.response.status_code}: {e.response.text[:200]}")
    except httpx.HTTPError as e:
        raise STTError(f"Could not reach the speech server ({url}): {e}")
    except (ValueError, KeyError, TypeError):
        raise STTError("The speech server did not return segments; does it support diarize=true?")

    return _clean_segments(segments)


def _clean_segments(segments: list) -> list:
    for s in segments:
        s["text"] = clean_transcription(s.get("text", ""))
    return [s for s in segments if s["text"]]


# ---------------------------------------------------------------------------
# meeting sessions: the recording streams to the speech server while the
# meeting runs, which transcribes it in windows, so stopping only leaves the
# last window to process. the server exposes them next to its transcription
# endpoint: <base>/audio/transcriptions -> <base>/meetings
# ---------------------------------------------------------------------------

def _meetings_url() -> str:
    url = _server_url().rstrip("/")
    suffix = "/audio/transcriptions"
    if not url.endswith(suffix):
        raise STTError("The speech server URL does not end in /audio/transcriptions, so it has no meeting sessions.")
    return url[:-len(suffix)] + "/meetings"


def _auth_headers() -> dict:
    key = _api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _meetings_call(method: str, path: str, timeout: httpx.Timeout, **kwargs):
    url = _meetings_url() + path
    try:
        async with httpx.AsyncClient(verify=_ssl_context(), timeout=timeout) as client:
            resp = await client.request(method, url, headers=_auth_headers(), **kwargs)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        raise STTError(f"Speech server error {e.response.status_code}: {e.response.text[:200]}")
    except httpx.HTTPError as e:
        raise STTError(f"Could not reach the speech server ({url}): {e}")
    except ValueError:
        raise STTError("The speech server returned an invalid response for a meeting session.")


async def meeting_session_start(max_speakers: int = None) -> str:
    """Opens a meeting session on the speech server and returns its id.

    max_speakers caps how many voices the server may find (e.g. the number of listed attendees)."""
    fields = {"max_speakers": str(max_speakers)} if max_speakers else {}
    data = await _meetings_call("POST", "", httpx.Timeout(10.0), data=fields)
    return data["id"]


async def meeting_session_append(session_id: str, part: int, filename: str, data: bytes, content_type: str,
                                 seq: int = None):
    """Sends the next piece of recorder part `part`. Pieces must be sent in order.

    seq numbers the pieces of a part from 0, so the server ignores a retried piece it already has."""
    fields = {"part": str(part)}
    if seq is not None:
        fields["seq"] = str(seq)
    await _meetings_call(
        "POST", f"/{session_id}/audio", httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0),
        data=fields, files={"file": (filename, data, content_type)}
    )


async def meeting_session_finish(session_id: str) -> list:
    """Transcribes what's left of the meeting and returns its segments [{start, end, speaker, text}]."""
    # normally just the last window, but a backlog (e.g. the server restarted its worker) can be longer
    timeout = httpx.Timeout(connect=5.0, read=1800.0, write=30.0, pool=5.0)
    data = await _meetings_call("POST", f"/{session_id}/finish", timeout, data={"response_format": "verbose_json"})
    try:
        return _clean_segments(data["segments"])
    except (KeyError, TypeError):
        raise STTError("The speech server did not return segments for the meeting.")


async def meeting_session_discard(session_id: str):
    try:
        await _meetings_call("DELETE", f"/{session_id}", httpx.Timeout(10.0))
    except STTError:
        pass  # best effort: the server drops idle sessions on its own
