"""
speech-to-text for openlumara

Three engines:
- local: a whisper.cpp `whisper-cli` subprocess. the binary and ggml model are
  auto-downloaded to data/stt/ on first use.
- server: an external whisper.cpp server (posts multipart to its /inference endpoint)
- openai: an OpenAI-compatible /audio/transcriptions endpoint on the main api url

the local engine is multi-user safe: all jobs serialize behind a single
semaphore, so a small single-core host isn't slammed by parallel transcriptions.
"""
import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

import httpx

import core
import core.config


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


def clean_transcription(text: str) -> str:
    """Strips non-speech artifacts ([Music], [BLANK_AUDIO], <|...|>, timestamps) from engine output"""
    if not text:
        return ""
    cleaned = _ARTIFACT_RE.sub(" ", text)
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
# local engine (whisper.cpp subprocess)
# ---------------------------------------------------------------------------

LOCAL_MODELS = {
    "tiny": "ggml-tiny.bin",
    "base": "ggml-base.bin",
    "small": "ggml-small.bin",
}

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{filename}"
RELEASES_API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases"


def _stt_dir() -> str:
    # user="" (not None) forces the GLOBAL data path: None would fall back to the
    # core.current_user contextvar and give each user their own engine, which would
    # re-download the binary + model per user. the engine is shared (one semaphore,
    # one model), so it lives in data/stt/ for everyone.
    path = core.get_data_path("stt", user="")
    os.makedirs(path, exist_ok=True)
    return path


def _engine_dir() -> str:
    """the engine dir holds the whisper binary AND its support files (on Windows the
    prebuilt exe needs the ggml/whisper DLLs sitting next to it), which is why the
    whole archive payload is extracted here, not just the exe"""
    path = os.path.join(_stt_dir(), "engine")
    os.makedirs(path, exist_ok=True)
    return path


def _binary_name() -> str:
    return "whisper-cli.exe" if platform.system() == "Windows" else "whisper-cli"


# asset names to look for per platform, newest naming scheme first. whisper.cpp
# has changed the asset names over time, and the newest release sometimes ships
# no binaries at all, so we walk the releases until one has a usable asset.
PLATFORM_ASSETS = {
    "linux-x64": ["whisper-bin-ubuntu-x64.tar.gz", "whisper-binaries-linux-x64.tar.gz"],
    "linux-aarch64": ["whisper-bin-ubuntu-arm64.tar.gz", "whisper-binaries-linux-aarch64.tar.gz"],
    "linux-armv7l": ["whisper-binaries-linux-armv7l.tar.gz"],
    "mac": ["whisper-binaries-mac.tar.gz"],
    "win-x64": ["whisper-bin-x64.zip", "whisper-binaries-windows-x64.zip"],
    "win-arm64": ["whisper-bin-win-cpu-arm64.zip", "whisper-bin-x64.zip", "whisper-binaries-windows-aarch64.zip"],
}


def _platform_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        if "aarch64" in machine or "arm64" in machine:
            return "linux-aarch64"
        if machine.startswith("armv7"):
            return "linux-armv7l"
        if machine in ("x86_64", "amd64"):
            return "linux-x64"
        raise STTError(f"Local STT is not supported on Linux {machine}")
    if system == "Darwin":
        return "mac"
    if system == "Windows":
        if machine in ("amd64", "x86_64"):
            return "win-x64"
        if machine == "arm64":
            return "win-arm64"
        raise STTError(f"Local STT is not supported on Windows {machine}")
    raise STTError(f"Local STT is not supported on {system} ({machine})")


def _github_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "openlumara-stt"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_whisper_asset():
    """Walks whisper.cpp releases (newest first) and returns (tag, asset name, download url) for this platform"""
    releases = _github_get(RELEASES_API + "?per_page=100")
    if not isinstance(releases, list):
        detail = str(releases.get("message", releases))[:200]
        raise STTError(f"Could not fetch whisper.cpp releases from GitHub ({detail})")

    wanted = PLATFORM_ASSETS[_platform_key()]
    for release in releases:
        by_name = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
        for name in wanted:
            if name in by_name:
                return release.get("tag_name", "?"), name, by_name[name]

    raise STTError(
        f"Could not find a whisper.cpp release with binaries for this platform "
        f"(looked for {', '.join(wanted)}). As a fallback, install whisper-cli "
        f"yourself and place it (plus a ggml model) in data/stt/."
    )


def _download(url: str, dest: str, name: str):
    """Downloads url to dest (via a .part file), logging progress in MB"""
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "openlumara-stt"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            total_mb = int(resp.headers.get("Content-Length", 0) or 0) >> 20
            last_mb = -1
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                mb = (f.tell() >> 20)
                if mb > last_mb:
                    last_mb = mb
                    _log(f"downloading {name}: {mb} MB{f' / {total_mb} MB' if total_mb else ''}...")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _extract_engine(archive: str, dest_dir: str) -> str:
    """Flattens a release archive into dest_dir and returns the path of the CLI binary"""
    target = _binary_name()

    def _safe_leaf(name: str):
        name = name.lstrip("/")
        if not name or ".." in name.split("/"):
            return None
        return name.split("/")[-1]

    # guard against absolute paths and traversal, and flatten subdirs (Release/...)
    # so the exe and its support files always end up side by side
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                leaf = _safe_leaf(member.filename)
                if leaf is None:
                    continue
                with zf.open(member) as src, open(os.path.join(dest_dir, leaf), "wb") as dst:
                    shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive) as tf:
            members = [m for m in tf.getmembers() if not m.isdir()]
            member_by_leaf = {}
            for m in members:
                leaf = _safe_leaf(m.name)
                if leaf is not None:
                    member_by_leaf.setdefault(leaf, m)

            for m in members:
                leaf = _safe_leaf(m.name)
                if leaf is None:
                    continue
                dest_path = os.path.join(dest_dir, leaf)

                # materialize links (possibly chained) as a copy of the real
                # target's content: no symlink permissions needed on any
                # platform, and SONAME names (eg libwhisper.so.1) end up as
                # concrete files next to the binary where the loader finds them
                src = m
                visited = {leaf}
                while src.issym() or src.islnk():
                    target_leaf = _safe_leaf(src.linkname)
                    if target_leaf is None or target_leaf in visited:
                        src = None
                        break
                    visited.add(target_leaf)
                    src = member_by_leaf.get(target_leaf)
                    if src is None:
                        break

                if src is None or not src.isfile():
                    continue
                with tf.extractfile(src) as f, open(dest_path, "wb") as dst:
                    shutil.copyfileobj(f, dst)

    binary_path = os.path.join(dest_dir, target)
    if not os.path.exists(binary_path):
        # older builds only ship `main`; if it's still around after a partial
        # install, rename it into place
        main_path = os.path.join(dest_dir, "main" + os.path.splitext(target)[1])
        if os.path.exists(main_path):
            os.replace(main_path, binary_path)

    if not os.path.exists(binary_path):
        raise STTError("whisper-cli binary not found in the downloaded release")
    os.chmod(binary_path, 0o755)

    # smoke test: a real whisper-cli answers --help. a stub or a binary missing
    # its support files doesn't.
    check = subprocess.run([binary_path, "--help"], capture_output=True, timeout=30, cwd=dest_dir)
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or b"").decode("utf-8", "replace").strip()[-200:]
        hint = ""
        if platform.system() == "Linux" and "cannot open shared object" in detail.lower():
            # the release bundles its own libraries, but OpenMP comes from the
            # system: fresh Debian/Ubuntu installs need one small package
            hint = " The host is missing a system library - on Debian/Ubuntu run: sudo apt-get install -y libgomp1"
        raise STTError(f"extracted whisper binary failed its smoke test ({detail}){hint}")

    return binary_path


def _install_local(size: str):
    """Downloads binary + model (blocking, runs in a thread) into data/stt/"""
    stt_dir = _stt_dir()
    binary = os.path.join(_engine_dir(), _binary_name())

    if not os.path.exists(binary):
        tag, asset_name, asset_url = _find_whisper_asset()
        _log(f"downloading whisper.cpp {tag} ({asset_name})...")
        archive = os.path.join(stt_dir, "whisper-release" + os.path.splitext(asset_name)[1])
        try:
            _download(asset_url, archive, "whisper.cpp binary")
            _extract_engine(archive, _engine_dir())
        finally:
            if os.path.exists(archive):
                os.remove(archive)

    model = os.path.join(stt_dir, LOCAL_MODELS[size])
    if not os.path.exists(model):
        _log(f"downloading whisper {size} model ({LOCAL_MODELS[size]})...")
        _download(MODEL_URL.format(filename=LOCAL_MODELS[size]), model, f"whisper {size} model")


def _local_ready(size: str) -> bool:
    has_binary = os.path.exists(os.path.join(_engine_dir(), _binary_name()))
    has_model = os.path.exists(os.path.join(_stt_dir(), LOCAL_MODELS[size]))
    return has_binary and has_model


_install_lock = asyncio.Lock()
_install_in_progress = False
_local_sem = None


def _local_sem_value() -> int:
    try:
        return max(1, int(_ch_cfg("stt_max_concurrent", default=1)))
    except (TypeError, ValueError):
        return 1


def _local_semaphore():
    global _local_sem
    if _local_sem is None:
        _local_sem = asyncio.Semaphore(_local_sem_value())
    return _local_sem


async def _ensure_installed(size: str):
    global _install_in_progress
    if _local_ready(size):
        return
    async with _install_lock:
        if not _local_ready(size):
            _install_in_progress = True
            try:
                await asyncio.to_thread(_install_local, size)
            finally:
                _install_in_progress = False


def _run_whisper(audio_data: bytes, binary: str, model: str, language: str, prompt: str, audio_format: str) -> str:
    """one blocking transcription (runs in a worker thread)"""
    timeout = 120
    try:
        timeout = max(10, int(_ch_cfg("stt_local_timeout", default=120)))
    except (TypeError, ValueError):
        pass

    with tempfile.TemporaryDirectory(prefix="stt-") as tmp_dir:
        in_path = os.path.join(tmp_dir, f"audio.{audio_format}")
        with open(in_path, "wb") as f:
            f.write(audio_data)

        out_path = os.path.join(tmp_dir, "out")
        txt_path = out_path + ".txt"

        last_detail = ""

        def _try(output_flags: list) -> bool:
            nonlocal last_detail
            # -p is --processors in current builds, so the prompt flag is long-form only
            cmd = [binary, "-m", model, "-f", in_path, "-of", out_path, "-np"] + output_flags
            if language and language.lower() != "auto":
                cmd += ["-l", language]
            if prompt:
                cmd += ["--prompt", prompt]

            _log(f"transcribing locally ({len(audio_data)} bytes, model={os.path.basename(model)})...")
            # cwd matters: whisper-cli writes its -of output files relative to cwd,
            # so run it inside the temp dir where in_path/out_path live
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=tmp_dir)
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()[-300:]
            if proc.returncode != 0 or not os.path.exists(txt_path):
                last_detail = detail
                return False
            return True

        # recent releases replaced `-ofmt txt` with `-otxt`; some builds exit 0 on
        # unrecognized args and some exit non-zero, so always give the fallback a
        # chance and let a missing output file be the failure signal
        if not _try(["-otxt"]):
            _try(["-ofmt", "txt"])
        if not os.path.exists(txt_path):
            raise STTError(f"whisper produced no output; the release CLI may have changed again ({last_detail})")

        with open(txt_path, "r", encoding="utf-8") as f:
            return f.read().strip()


async def transcribe_local(audio_data: bytes, language: str = "auto",
                           prompt: str = None, audio_format: str = "wav") -> str:
    size = _ch_cfg("stt_model", default="tiny") or "tiny"
    if size not in LOCAL_MODELS:
        size = "tiny"
    if not _local_ready(size) and _install_in_progress:
        raise STTError("The local STT engine is still downloading (first use). Try again in a minute.")
    binary = os.path.join(_engine_dir(), _binary_name())
    model = os.path.join(_stt_dir(), LOCAL_MODELS[size])
    await _ensure_installed(size)
    async with _local_semaphore():
        return await asyncio.to_thread(_run_whisper, audio_data, binary, model, language, prompt, audio_format)


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
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)) as client:
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)) as client:
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

async def transcribe(audio_data: bytes, purpose: str = "preview", language: str = "auto",
                     prompt: str = None, audio_format: str = "wav") -> str:
    """Transcribes audio, picking an engine based on config and purpose.

    purpose "preview" = short live window while the user is still talking.
    purpose "commit" = a final segment, safe to spend time on the higher-quality engine.
    """
    text = await _route(audio_data, purpose, language, prompt, audio_format)
    return clean_transcription(text)


async def _route(audio_data: bytes, purpose: str, language: str,
                 prompt: str, audio_format: str) -> str:
    engine = _ch_cfg("stt_engine", default="auto") or "auto"
    server_url = _server_url()
    api_url = (core.config.get("api", "url", default="") or "").strip()
    has_openai = bool(api_url) and api_url != "http://API_URL_HERE/v1"
    size = _ch_cfg("stt_model", default="tiny") or "tiny"

    if engine == "server":
        if server_url:
            return await transcribe_server(audio_data, server_url, language, prompt, audio_format)
        if has_openai:
            return await transcribe_openai(audio_data, language, prompt, audio_format)
        raise STTError(
            "stt_engine is set to 'server' but no whisper server URL is configured. "
            "Set the Voice URL in Settings -> Api or stt_whisper_server_url in Settings -> Channels -> webui."
        )

    if engine == "local":
        return await transcribe_local(audio_data, language, prompt, audio_format)

    # auto: live previews prefer the local engine (fast, no round-trip to the big server),
    # if it's already installed. final commits prefer an explicitly configured whisper
    # server, then the local engine (when installed) - the OpenAI-compatible endpoint on
    # api.url is a last-resort fallback, since a main LLM endpoint without audio support
    # rejects /audio/transcriptions.
    if purpose == "preview":
        if _local_ready(size):
            return await transcribe_local(audio_data, language, prompt, audio_format)
        if server_url:
            return await transcribe_server(audio_data, server_url, language, prompt, audio_format)
        if has_openai:
            return await transcribe_openai(audio_data, language, prompt, audio_format)
        raise STTError(
            "No STT engine available. The local engine downloads on first use at final commit; "
            "configure the Voice URL in Settings -> Api (or stt_whisper_server_url in "
            "Settings -> Channels -> webui) for previews right now."
        )

    if server_url:
        return await transcribe_server(audio_data, server_url, language, prompt, audio_format)
    if _local_ready(size):
        return await transcribe_local(audio_data, language, prompt, audio_format)
    if has_openai:
        return await transcribe_openai(audio_data, language, prompt, audio_format)
    return await transcribe_local(audio_data, language, prompt, audio_format)
