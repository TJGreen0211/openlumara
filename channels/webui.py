"""
OpenLumara WebUI - manual rewrite

This is daunting, but i'm rewriting the entire WebUI from the ground up, manually, with minimal AI-generated code, due to high amount of unpredictable bugs in the previous version, and sheer difficulty of maintaining it

The plan is to use FastAPI for the backend again, but manually written, and alpine.js for the frontend, since it's nice and lightweight and not React.

Let's get this WebUI up to the standards of the rest of openlumara, since it's become basically the primary way everyone uses it..

~ Rose22
"""

# openlumara core
import core

# system
import os
import json
import asyncio
import collections
import datetime
import hashlib
import time

# webui stuff
import fastapi, fastapi.templating, fastapi.staticfiles
import starlette, starlette.middleware.sessions, starlette.middleware.gzip
import itsdangerous
import uvicorn
import base64
import zoneinfo
import urllib.parse

# security libraries
import secrets

# --------------------
# Channel class
# --------------------
class Webui(core.channel.Channel):
    """A full-featured, modern webUI for OpenLumara, providing you with a privacy-friendly option that doesn't depend on any external chat providers"""
    version = 2.0

    dependencies = [
        "fastapi",
        "starlette>=1.0.1",
        "itsdangerous",
        "websockets",
        "jinja2",
        "uvicorn",
        "python-multipart",
        "bcrypt",
        "httpx"
    ]

    # these settings are taken straight from the previous webUI,
    # and currently, many of the settings don't do anything yet
    # but i plan to support these of course
    settings = {
        "network_mode": {
            "type": "select",
            "options": {
                "local": "Allows only the device OpenLumara is running on to access the WebUI (sets hostname to `localhost`)",
                "internet": "Allows any device to access the WebUI (sets hostname to `0.0.0.0`)",
                "custom": "Use the custom hostname defined below"
            },
            "default": "local"
        },
        "custom_host": {
            "default": None,
            "depends": {"network_mode": "custom"}
        },
        "port": {
            "description": "What port to run the WebUI on. Set this to 80 to be able to access it like a normal website, and anything else to access it on that port (for example http://yourdomain.org:3000)",
            "default": 3000
        },
        "allow_admin_commands": {
            "description": "Whether to allow /commands that control the openlumara server. Turn this off if you expose your openlumara instance to the internet without a login!",
            "default": True
        },
        "enable_chat_header": {
            "description": "Whether to enable the header at the top of a chat. Disabling this removes access to all graphical controls, and strips the interface down to a very basic chat. You might want this for public instances!",
            "default": True
        },
        "enable_title": {
            "default": True,
            "description": "Whether to show a fancy title in the header",
            "depends": "enable_chat_header"
        },
        "title": {
            "default": "TJ GPT",
            "depends": {"enable_chat_header": True, "enable_title": True}
        },
        "enable_chat_titlebar": {
            "description": "Whether to show the name of the chat below the header",
            "default": False
        },
        "enable_sidebar": {
            "description": "Whether to enable the sidebar at the left of the screen. Without it, you can\'t switch chats the graphical way, but you can still use commands like `/chat`!",
            "default": True
        },
        "show_unsafe_settings": {
            "description": "Whether to show unsafe settings. This setting has to be manually toggled via `/config` or by editing the config file, because if you want access to the unsafe features, you hopefully know what you're doing!",
            "default": False,
            "unsafe": True
        },
        "require_login": {
            "description": "Whether to protect the WebUI with a username and password. **Highly recommended if your webui is exposed to the internet!!**",
            "default": False
        },
        "login_lifetime": {
            "description": "How many days to stay logged in for",
            "default": 30,
            "depends": "require_login"
        },
        "stt_engine": {
            "type": "select",
            "options": {
                "auto": "Local engine for live previews, whisper server (if configured) for final commits",
                "local": "Built-in local engine (runs whisper.cpp in a subprocess; binary + model auto-download on first use)",
                "server": "External whisper.cpp server only (URL below, or the main API url if it's an OpenAI-compatible STT endpoint)"
            },
            "default": "auto",
            "description": "Which speech-to-text engine to use for microphone voice input."
        },
        "stt_model": {
            "type": "select",
            "options": {
                "tiny": "Fastest, lightest (~75 MB download, ~0.5 GB RAM) - recommended for small hosts",
                "base": "Better accuracy, still light (~142 MB download, ~1 GB RAM)",
                "small": "Best accuracy of the local engines (~466 MB download, ~1.5 GB RAM)"
            },
            "default": "tiny",
            "description": "Size of the local whisper model. Only used by the local / auto engines."
        },
        "stt_whisper_server_url": {
            "type": "text",
            "default": "",
            "description": "Full URL of your whisper.cpp server's /inference endpoint (for example http://100.x.x.x:8080/inference). Used by the 'server' engine and for final commits with 'auto'. Leave empty to use the main API url instead."
        },
        "stt_max_concurrent": {
            "type": "select",
            "options": {
                "1": "1 (safe on a single-core host)",
                "2": "2",
                "4": "4"
            },
            "default": "1",
            "description": "How many local whisper jobs may run at once. Each job uses one whisper model worth of RAM plus a core's worth of CPU."
        },
        "stt_local_timeout": {
            "type": "select",
            "options": {
                "60": "60s",
                "120": "120s",
                "300": "300s"
            },
            "default": "120",
            "description": "Maximum time a single local transcription may take before it's killed. Increase this if you dictate long passages on a slow host."
        },
        "voice_preview_style": {
            "type": "select",
            "options": {
                "strip": "Live transcription shows in a caption strip above the input bar. "
                         "The input field only receives finished segments",
                "off": "No live transcription while recording - the input field only "
                       "updates when a segment is committed"
            },
            "default": "strip",
            "description": "How the live voice preview is displayed while recording. "
                           "The input field itself never shows the flickering live tail either way."
        }
    }

    async def on_ready(self):
        # paths
        self.path = core.get_path(os.path.join("channels", "webui"))
        self.template_path = os.path.join(self.path, "templates")
        self.assets_path = os.path.join(self.path, "assets")

        # fastapi-specific instances
        self.templates = fastapi.templating.Jinja2Templates(self.template_path)

        # aaand create it
        self.app = await create_fastapi(self)

        # determine network mode
        network_mode = self.config.get("network_mode")
        match network_mode:
            case "local":
                self.host = "127.0.0.1"
            case "internet":
                self.host = "0.0.0.0"
            case "custom":
                self.host = self.config.get("custom_host")
            case _:
                self.host = "127.0.0.1"

        self.port = self.config.get("port")
        self.url = f"http://{self.host}:{self.port}"

        # stores logs from channel.log() (the old list was re-sliced on every line)
        self.logs = collections.deque(maxlen=1000)

        # service worker data. computed once per server run: the old code
        # walked the entire webui folder on every /sw.js request
        self._sw_version = self._compute_cache_version()
        self._sw_assets = self._compute_sw_assets()

        self.login_attempts = {}

        # per-user context instances
        self.user_contexts = {}

        # user management
        self.user_manager = core.auth.UserManager(core.get_data_path())

        # initialize the websocket manager
        self.websocket_manager = WebSocketManager(self)

    @property
    def context(self):
        """Return per-user context when current_user is set, otherwise default."""
        username = core.current_user.get()
        if username and username in self.user_contexts:
            return self.user_contexts[username]
        return self._default_context

    def _commands_authorized_for(self, username):
        """Whether this user may run admin /commands (restart, coder, debug...) from chat.

        single-user mode (login disabled) keeps the instance-wide
        allow_admin_commands switch. in multi-user mode, admin commands are
        gated by role: regular users must not be able to restart the server
        or change instance state, regardless of allow_admin_commands.
        """
        if not self.config.get("require_login", False):
            return self.config.get("allow_admin_commands")

        if not username:
            return False

        try:
            role = self.user_manager.get_user(username).get("role")
        except Exception:
            return False
        return role == "admin" and self.config.get("allow_admin_commands")

    def websocket_origin_allowed(self, websocket):
        """Reject cross-origin WebSocket handshakes.

        browsers do not apply CORS to WebSockets, so without this check any
        page the user visits could open ws://host:port/ws and drive the agent
        (including /commands) as if it were the WebUI. browsers always send
        Origin on WebSocket handshakes, so a missing Origin means a
        non-browser client and is allowed; a present Origin must be
        same-origin (Host / X-Forwarded-Host) or local.
        """
        origin = websocket.headers.get("origin")
        if not origin:
            return True
        try:
            origin_host = urllib.parse.urlsplit(origin).hostname
        except ValueError:
            return False
        if not origin_host:
            return False
        origin_host = origin_host.lower()
        if origin_host in ("localhost", "127.0.0.1", "::1"):
            return True
        allowed = set()
        for header in ("host", "x-forwarded-host"):
            value = websocket.headers.get(header)
            if value:
                host = urllib.parse.urlsplit(f"//{value}").hostname
                if host:
                    allowed.add(host.lower())
        if self.host and self.host not in ("0.0.0.0", "::"):
            allowed.add(self.host.lower())
        return origin_host in allowed

    def _get_session_secret(self):
        """Server-wide session signing secret, generated on first run.

        the previous code passed config.get("session_secret", "literal") to
        SessionMiddleware, but ConfigManager.get treats a trailing non-empty
        string as another key, not a default, so this returned None and
        starlette signed sessions with str(None) == "None" - anyone could
        forge an authenticated admin cookie. the secret now lives in the
        global data dir (user="" bypasses the per-user contextvar, sessions
        are server-wide), and deleting the file rotates it (logs everyone
        out).
        """
        data_path = core.get_data_path(user="")
        secret_file = os.path.join(data_path, "session_secret")
        try:
            with open(secret_file, "r", encoding="utf-8") as f:
                secret = f.read().strip()
            if secret:
                return secret
        except FileNotFoundError:
            pass
        secret = secrets.token_hex(32)
        try:
            # 0600: the signing key must not be readable by other local users,
            # or they can forge authenticated admin session cookies. chmod
            # also tightens a pre-existing file that was created world-readable
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secret)
            os.chmod(secret_file, 0o600)
        except OSError as e:
            self.log("webui", f"Could not persist session secret, using an ephemeral one: {e}")
        return secret

    async def _get_user_context(self, username):
        """Get or create Context for a user."""
        if username not in self.user_contexts:
            self.user_contexts[username] = core.context.Context(self, username=username)
            await self.user_contexts[username].chat.autoload()
        return self.user_contexts[username]

    async def run(self):
        # on_log can be called from worker threads; it hands log lines to this loop
        self._loop = asyncio.get_running_loop()

        self.log("webui", f"Starting WebUI on {self.url}")

        # serve the app using uvicorn
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,

            # this makes it work in situations where https and http content are served mixed
            proxy_headers=True,
            forwarded_allow_ips = "127.0.0.1",

            # the 16MB default kills the whole websocket connection when a
            # websocket message (e.g. a chat send with several photos base64'd
            # inline) exceeds it, silently dropping every file in the message.
            # allow up to 128MB so multi-file uploads arrive intact.
            ws_max_size=128 * 1024 * 1024,

            # protocol pings find dead connections. the 20s default timeout is
            # tight for phones on mobile data; a stream keeps running across a
            # drop now anyway, so tolerating a slow pong costs nothing
            ws_ping_interval=20,
            ws_ping_timeout=30,

            # only log critical http errors
            log_level="error"
        )
        self.server = uvicorn.Server(config)

        await self.server.serve()

    def _compute_cache_version(self):
        # generate an sw.js cache version by hashing the *content* of every file
        # the webui serves (this file + assets + templates), because bumping
        # sw.js's version manually each time i update the webui is a total pain
        # and i don't want to deal with it

        # the old version was the latest file mtime, which never changed when a
        # deploy preserved timestamps (docker builds, rsync, some git checkouts).
        # with a cache-first service worker and version-less asset urls, that
        # meant phones kept serving the old cached CSS/JS forever after a deploy
        # (e.g. the pre-fix meeting/voice layout). a content hash changes
        # whenever the served files change, so the new service worker activates
        # and clears the stale cache

        hasher = hashlib.sha256()
        try:
            with open(__file__, "rb") as f:
                hasher.update(f.read())
        except (OSError, FileNotFoundError):
            pass

        webui_dir = core.get_path("channels/webui")
        for root, dirs, files in os.walk(webui_dir):
            for file in sorted(files):
                file_path = os.path.join(root, file)
                try:
                    rel_path = os.path.relpath(file_path, webui_dir).replace(os.sep, "/")
                    hasher.update(rel_path.encode("utf-8"))
                    with open(file_path, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            hasher.update(chunk)
                except (OSError, FileNotFoundError):
                    # Skip files that can't be accessed
                    pass

        return "v" + hasher.hexdigest()[:16]

    def _compute_sw_assets(self):
        # list of assets to precache, relative to the /assets mount point.
        # (the old code scanned a nonexistent 'static/' folder, so the
        # service worker silently cached nothing)
        files_to_cache = []
        assets_dir = os.path.join(self.path, "assets")
        for subdir in ['js', 'css']:
            dir_path = os.path.join(assets_dir, subdir)
            if os.path.isdir(dir_path):
                for root, _, files in os.walk(dir_path):
                    for filename in files:
                        full_path = os.path.join(root, filename)
                        rel_path = os.path.relpath(full_path, assets_dir)
                        # the templates link assets as ?v=<content hash>, so the
                        # precache entries must carry the same query or the
                        # offline fallback would never match a request
                        files_to_cache.append('/assets/' + rel_path.replace(os.sep, '/') + '?v=' + self._sw_version)
        files_to_cache.sort()
        return files_to_cache

    async def on_push(self, message, username=None):
        # deliver the push only to the user it belongs to (None = everyone).
        # username is captured at push() time and carried through the push queue,
        # because this handler runs in the push queue's own background task
        await self.websocket_manager.broadcast({
            "type": "push",
            "content": message
        }, username=username)

    async def push(self, message, username=None):
        """persist the pushed message into the owning user's chat.

        without this override a push for a user whose context isn't currently
        cached (offline user, e.g. a calendar notification after a restart)
        would be persisted into the shared default chat instead of the user's
        own history - leaking their content into a file shared by everyone.
        """
        if not hasattr(self, "push_queue"):
            return False

        if username is None:
            username = core.current_user.get()

        uctx = None
        if username:
            try:
                if self.user_manager.get_user(username):
                    # pin the contextvar to the target user while resolving
                    # their context, so per-user storage paths are computed
                    # for the right owner (the caller's contextvar may belong
                    # to a different user or be unset)
                    token = core.current_user.set(username)
                    try:
                        uctx = await self._get_user_context(username)
                    finally:
                        core.current_user.reset(token)
            except Exception:
                uctx = None

        if uctx is None:
            uctx = self._default_context

        if not isinstance(message, dict):
            message = {"role": "assistant", "content": str(message)}

        await uctx.chat.messages.add(message)
        await self.push_queue.put((message, username))

    def on_log(self, category, message):
        if not hasattr(self, 'websocket_manager'):
            # not initialized yet
            return False

        # Store log in buffer for history (the deque caps it at 1000 entries)
        self.logs.append({"category": category, "message": message})

        # Broadcast log messages: admin-only in multi-user mode (logs may
        # contain other users' content), to everyone in single-user mode.
        # queueing onto the connections' outboxes is synchronous, so no task
        # per log line is needed
        log_message = {
            "type": "log",
            "category": category,
            "message": message
        }
        admins_only = bool(self.config.get("require_login", False))
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        loop = getattr(self, "_loop", None)
        if running is not None and running is loop:
            self.websocket_manager.broadcast_nowait(log_message, admins_only=admins_only)
        elif loop is not None and not loop.is_closed():
            # logged from a worker thread: the outbox queues belong to the loop
            loop.call_soon_threadsafe(self.websocket_manager.broadcast_nowait, log_message, None, admins_only)

    async def on_shutdown(self):
        # broadcast first so clients know we're going away (and give the
        # outboxes a moment to actually send it)
        await self.websocket_manager.broadcast({"type": "shutdown"})
        await self.websocket_manager.drain(timeout=1.0)

        # flush any pending debounced message saves for every cached user
        # context (the base Channel._shutdown only flushes the default
        # context), so a restart within the debounce window of a user's
        # last message can't lose it
        for uctx in list(getattr(self, "user_contexts", {}).values()):
            try:
                if uctx.chat.messages:
                    await uctx.chat.messages.save()
            except Exception:
                pass

        # then properly stop uvicorn
        # this is a flag exposed by uvicorn itself, which causes it to start gracefully shutting down when set
        self.server.should_exit = True

        # wait for uvicorn to actually finish shutting down
        try:
            await asyncio.wait_for(self.server.shutdown(), timeout=5.0)
        except (AttributeError, asyncio.TimeoutError):
            # fallback: just give it a moment to release the socket
            await asyncio.sleep(0.5)

# -------------------
# Helper Functions
# -------------------
def serialize_for_json(obj):
    """Recursively converts non-serializable objects into plain dicts/lists."""
    if isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_for_json(x) for x in obj]
    elif hasattr(obj, 'to_dict'):
        return serialize_for_json(obj.to_dict())
    elif hasattr(obj, '__dict__'):
        return serialize_for_json(obj.__dict__)
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(obj)

def get_recursive_assets(assets_path, ext, skip: list = []):
    """Recursively list asset files with paths relative to server root."""
    files = []

    for root, dirs, filenames in os.walk(assets_path):
        # Skip files and directories marked for skipping
        filenames[:] = [f for f in filenames if os.path.basename(f) not in skip]
        dirs[:] = [d for d in dirs if d not in skip]

        for filename in filenames:
            if filename.endswith(f".{ext}"):
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, assets_path)
                files.append(rel_path)

    return sorted(files)

# upload size caps, checked on the base64 strings BEFORE decoding. without a
# cap, one huge websocket message amplifies into many simultaneous copies
# (json.loads copy, decoded bytes, re-encoded block, storage dump, api dump,
# broadcast) and then lives in the chat history forever.
MAX_UPLOAD_FILE_B64 = 32 * 1024 * 1024    # per file, ~24MB raw
MAX_UPLOAD_TOTAL_B64 = 96 * 1024 * 1024   # per message, ~72MB raw

def upload_size_error(files_data):
    """Returns an error message if attached files exceed the size caps, else None."""
    if not files_data:
        return None

    for f in files_data:
        if len(f.get("data") or "") > MAX_UPLOAD_FILE_B64:
            return (f"File '{f.get('name')}' is too large. "
                    f"Each file must stay under {MAX_UPLOAD_FILE_B64 // 1048576}MB.")

    total = sum(len(f.get("data") or "") for f in files_data)
    if total > MAX_UPLOAD_TOTAL_B64:
        return (f"Attached files are too large together ({total // 1048576}MB). "
                f"One message is limited to {MAX_UPLOAD_TOTAL_B64 // 1048576}MB total.")

    return None

async def decode_upload_files(files_data):
    """Decodes uploaded files into an ordered list of (name, bytes) pairs.

    a list, not a dict, so files that share a name aren't silently dropped.
    decoding tens of megabytes of base64 runs in a thread, so it doesn't
    freeze the event loop (every other user's stream) while it works
    """
    def decode():
        return [(f["name"], base64.b64decode(f["data"])) for f in files_data]
    return await asyncio.to_thread(decode)

async def save_attachments_to_site(coder, site, files_list):
    """
    copies a message's image/video attachments into the site that's open in the
    preview panel, so the AI can reference them from the html (vision blocks
    alone give it nothing to link to, and are stripped from older messages).
    returns a note for the AI listing where they landed, or None.
    """
    if not coder or not isinstance(site, dict) or not files_list:
        return None
    sandbox = site.get("sandbox")
    folder = site.get("folder") or ""
    if not isinstance(sandbox, str) or not sandbox or not isinstance(folder, str):
        return None

    saved = []
    for filename, data in files_list:
        if not coder._sniff_media(data):
            continue  # documents, audio etc. are only for the AI to read
        try:
            result = await coder._save_site_media(sandbox, folder, filename, data)
        except Exception as e:
            core.log("webui", f"could not save attachment '{filename}' to site: {core.detail_error(e)}")
            continue
        saved.append(f"- {filename} → {result['path']}")

    if not saved:
        return None

    where = f"'{folder}'" if folder else "the sandbox root"
    return (f"[The attached media was saved into the site in {where} (sandbox '{sandbox}'). "
            f"Use these paths, relative to that folder:\n" + "\n".join(saved) + "]")

def inject_indexes_into_messages(lst: list):
    """speaks for itself lol"""
    return [{**dickt, 'index': index} for index, dickt in enumerate(lst)]

def inject_indexes_into_chat(chat):
    """injects indexes into a chat's messages"""
    # copy it so we dont mutate it when injecting indexes
    chat_copy = dict(chat)

    # insert indexes into the messages array
    # so that the UI can track them for things like
    # editing messages, regenerating, deleting, etc
    chat_copy["messages"] = inject_indexes_into_messages(chat["messages"])

    return chat_copy

class SelectiveGZipMiddleware:
    """gzips the page, assets and API JSON (a long chat's history shrinks ~5-8x).

    previewed site files and media are left alone: they're mostly images and
    video that are already compressed, and gzipping a ranged (206) video
    response breaks seeking"""

    SKIP_PREFIXES = ("/api/artifact/", "/api/site-media", "/icon-", "/favicon")

    def __init__(self, app):
        self.app = app
        self.gzip = starlette.middleware.gzip.GZipMiddleware(app, minimum_size=1000, compresslevel=6)

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and not scope["path"].startswith(self.SKIP_PREFIXES)
            and not any(name == b"range" for name, _ in scope["headers"])
        ):
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)

# -------------------
# FastAPI creator (contains routes and so on)
# -------------------
def api_result(obj = None, success: bool = True):
    if obj is None:
        result = {}
    else:
        result = obj

    return {"data": result, "success": success}

async def create_fastapi(channel):
    app = fastapi.FastAPI()

    # add authorization, cookies, and so on (middleware)
    # auth middleware for all routes
    @app.middleware("http")
    async def auth_middleware(request: fastapi.Request, call_next):
        # Skip auth check if login isn't required
        if not channel.config.get("require_login", False):
            return await call_next(request)

        # Skip auth for login page and assets
        if request.url.path in ["/login", "/logout"] or str(request.url.path).startswith("/assets/"):
            return await call_next(request)

        # previewed site files authenticate with a signed token in their url
        # instead: the preview iframe has an opaque origin, so requests the
        # page itself makes (images, css, links) never carry the session cookie
        if str(request.url.path).startswith("/api/artifact/"):
            return await call_next(request)

        # Check session for API and other routes
        if not request.session.get("authenticated", False):
            # For API requests, return 401
            if str(request.url.path).startswith("/api"):
                return fastapi.responses.JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"}
                )
            # For web routes, redirect to login
            if request.url.path != "/login":
                return fastapi.responses.RedirectResponse(url="/login", status_code=303)

        # Set user context for per-user data isolation
        username = request.session.get("username")
        role = request.session.get("role", "user")
        if username:
            core.current_user.set(username)
            request.state.username = username
            request.state.role = role

        return await call_next(request)

    session_lifetime_days = channel.config.get("login_lifetime")
    app.add_middleware(
        starlette.middleware.sessions.SessionMiddleware,
        secret_key=channel._get_session_secret(),
        max_age=session_lifetime_days * 86400
    )

    app.add_middleware(SelectiveGZipMiddleware)

    @app.middleware("http")
    async def asset_cache_headers(request: fastapi.Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            if request.query_params.get("v"):
                # the templates link assets as ?v=<content hash of the webui>, so a
                # versioned url never changes content: let the browser keep it
                # instead of re-checking ~70 files on every page load
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                # unversioned (e.g. code themes swapped in at runtime): revalidate via etag
                response.headers["Cache-Control"] = "no-cache"
        return response

    # serve asset files (formerly /static) using fastAPI's mount()
    app.mount("/assets", fastapi.staticfiles.StaticFiles(directory=channel.assets_path), name="assets")

    # ------------------
    # Web pages
    # ------------------

    # main page
    @app.get("/")
    async def root(request: fastapi.Request):
        """The main page. This returns HTML, not JSON"""
        css_files = get_recursive_assets(os.path.join(channel.assets_path, "css"), "css", skip=["code-themes"])
        alpine_stores = os.listdir(os.path.join(channel.assets_path, "js", "stores"))
        js_utils = os.listdir(os.path.join(channel.assets_path, "js", "utils"))
        js_files = get_recursive_assets(os.path.join(channel.assets_path, "js"), "js", skip=["init.js", "stores", "libs", "utils"])

        return channel.templates.TemplateResponse(request, "index.html", {
            "version": channel.version,
            "config": channel.config,
            "asset_version": channel._sw_version,
            "css_files": css_files,
            "alpine_stores": alpine_stores,
            "js_utils": js_utils,
            "js_files": js_files,
            "login_enabled": channel.config.get("require_login")
        })

    # ---- login
    # -- GET
    @app.get("/login")
    async def login_page(request: fastapi.Request):
        """Shows the login form."""
        if channel.config.get("require_login"):
            return channel.templates.TemplateResponse(request, "login.html", {
                "error": None, "asset_version": channel._sw_version})
        else:
            return fastapi.responses.RedirectResponse(url="/", status_code=303)

    # -- POST
    @app.post("/login")
    async def login_submit(request: fastapi.Request):
        """Handles login form submission."""

        # rate limit the request
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        if client_ip in channel.login_attempts:
            # clean old attempts (older than 15 minutes)
            channel.login_attempts[client_ip] = [
                t for t in channel.login_attempts[client_ip] if now - t < 900
            ]

            if len(channel.login_attempts[client_ip]) >= 5:
                return fastapi.responses.JSONResponse(
                    status_code=429,
                    content={"error": "Too many attempts. Try again later."}
                )

        # and now check if the credentials match
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

        if not username or not password:
            return channel.templates.TemplateResponse(request, "login.html", {
                "error": "Invalid credentials", "asset_version": channel._sw_version})

        auth_result = channel.user_manager.authenticate(username, password)
        if auth_result:
            channel.login_attempts[client_ip] = []
            request.session.clear()
            request.session["authenticated"] = True
            request.session["username"] = auth_result["username"]
            request.session["role"] = auth_result["role"]
            request.session["_csrf"] = secrets.token_hex(16)

            return fastapi.responses.RedirectResponse(url="/", status_code=303)

        # on failure, record the login attempt
        if client_ip not in channel.login_attempts:
            channel.login_attempts[client_ip] = []
        channel.login_attempts[client_ip].append(now)

        return channel.templates.TemplateResponse(request, "login.html", {
            "error": "Invalid credentials", "asset_version": channel._sw_version})

    # ---- logout
    @app.get("/logout")
    async def logout(request: fastapi.Request):
        """Logs the user out by clearing their session."""
        username = request.session.get("username")
        sid = request.session.get("_csrf")
        if username and hasattr(channel, 'user_contexts'):
            channel.user_contexts.pop(username, None)
        request.session.clear()
        # an already-open socket was authenticated at handshake time and would
        # otherwise keep working after logout. only this session's sockets:
        # the user's other devices stay logged in
        if sid:
            await channel.websocket_manager.close_where(sid=sid, code=4001, reason="Logged out")
        return fastapi.responses.RedirectResponse(url="/login", status_code=303)

    # ------------------
    # API routes (/api)
    # ------------------

    # reminder to self: docstrings show up in the autogenerated API docs (/docs), so they are essential

    async def _get_uctx(request):
        """Helper: get user context and username from request session."""
        username = request.session.get("username")
        if username:
            core.current_user.set(username)
            return await channel._get_user_context(username), username
        return channel.context, None

    def _current_chat_id(uctx):
        # chat.get() raises when no chat is loaded yet
        try:
            return uctx.chat.get("id")
        except Exception:
            return None

    # --- chats
    # -- GET
    @app.get("/api/chat/load/{chat_id}")
    async def chat_load(chat_id: str, request: fastapi.Request):
        """Loads a specific chat by its id"""
        uctx, username = await _get_uctx(request)
        if chat_id != _current_chat_id(uctx):
            # a stream still writing into the current chat would continue
            # into the loaded one
            await channel.websocket_manager.cancel_stream(username)
        try:
            success = await uctx.chat.load(chat_id)
        except Exception as e:
            return api_result(f"error while loading chat: {core.detail_error(e)}", success=False)

        if not success:
            chat = dict(uctx.chat.get())
            chat["turn_history"] = await channel.group_history(await uctx.chat.messages.get())
            return api_result(chat, success=True)

        # origin lets the tab that asked for the switch ignore its own echo
        await channel.websocket_manager.broadcast({
            "type": "chat_switched",
            "id": chat_id,
            "origin": request.headers.get("x-client-id"),
        }, username=username)

        chat = dict(uctx.chat.get())
        chat["turn_history"] = await channel.group_history(await uctx.chat.messages.get())
        return api_result(chat, success=True)

    @app.get("/api/chat/current")
    async def chat_get_current(request: fastapi.Request):
        """Gives you the currently loaded chat's data"""
        uctx, _ = await _get_uctx(request)

        try:
            chat = dict(uctx.chat.get())
        except Exception:
            # No chat loaded yet, create a new one
            await uctx.chat.new()
            chat = dict(uctx.chat.get())

        chat["turn_history"] = await channel.group_history(await uctx.chat.messages.get())
        return api_result(chat)

    @app.get("/api/chat/export")
    async def chat_export(request: fastapi.Request):
        """Gives you the chat history as a human-readable string, which you can save to a file or do whatever else with"""
        uctx, _ = await _get_uctx(request)
        return api_result(await uctx.chat.export())

    @app.get("/api/chats")
    async def get_chats(request: fastapi.Request):
        """Returns a list of all chats, with pagination"""
        uctx, _ = await _get_uctx(request)
        offset = int(request.query_params.get("offset", 0))
        limit = int(request.query_params.get("limit", 50))
        category = request.query_params.get("category", None)

        all_chats = uctx.chat.get_all()
        if category:
            all_chats = [c for c in all_chats if c.get("category") == category]

        paginated = all_chats[offset:offset + limit]
        has_more = offset + limit < len(all_chats)

        return api_result({"messages": paginated, "has_more": has_more}, success=True)

    @app.get("/api/chats/categories")
    async def get_chat_categories(request: fastapi.Request):
        """Returns a list of all existing chat categories"""
        uctx, _ = await _get_uctx(request)
        return api_result(uctx.chat.get_categories(), True)

    @app.get("/api/chats/category_counts")
    async def get_chat_category_counts(request: fastapi.Request):
        """Returns how many chats each category holds, as {category: count}"""
        uctx, _ = await _get_uctx(request)
        counts = {}
        for chat in uctx.chat.get_all():
            category = chat.get("category")
            counts[category] = counts.get(category, 0) + 1
        return api_result(counts, True)

    @app.post("/api/chats/search")
    async def search_chats(request: fastapi.Request):
        """Searches across all chats for messages matching a query"""
        uctx, _ = await _get_uctx(request)
        data = await request.json()
        query = data.get("query", "").strip()
        search_in_content = data.get("search_in_content", True)
        category = data.get("category")

        if not query:
            return api_result([])

        results = await uctx.chat.search(query)

        if category and category != 'general':
            results = [r for r in results if r.get('category') == category]
        elif category == 'general':
            results = [r for r in results if not r.get('category') or r.get('category') == 'general']

        return api_result(results)

    @app.get("/api/chat/prompt")
    async def get_prompt(request: fastapi.Request):
        uctx, _ = await _get_uctx(request)
        sysprompt = await uctx.get(system_prompt=True, end_prompt=False, history=False)
        if isinstance(sysprompt, core.api.APIError):
            return api_result(sysprompt, success=False)

        return api_result(sysprompt[-1].get("content"))

    # -- POST
    @app.post("/api/chat/new")
    async def chat_new(request: fastapi.Request):
        """Creates a new chat"""
        uctx, username = await _get_uctx(request)
        # the new chat becomes current: stop a stream that would continue into it
        await channel.websocket_manager.cancel_stream(username)
        category = "general"
        title = None
        metadata = None
        try:
            body = await request.json()
            if body:
                category = body.get("category", "general")
                title = body.get("title")
                metadata = body.get("metadata")
        except Exception:
            pass

        kwargs = {"category": category}
        if title:
            kwargs["title"] = title
        if isinstance(metadata, dict):
            kwargs["metadata"] = metadata

        return api_result(await uctx.chat.new(**kwargs))

    @app.post("/api/chat/rename/{chat_id}")
    async def chat_rename(chat_id: str, request: fastapi.Request):
        """Renames a chat by its ID"""
        uctx, _ = await _get_uctx(request)
        try:
            data = await request.json()
            new_title = data.get('title', '').strip()
            if not new_title:
                return api_result("Title cannot be empty", success=False)

            index = uctx.chat._find_index(chat_id)
            if index is None:
                return api_result("Chat not found", success=False)

            await uctx.chat.set("title", new_title, index=index)

            return api_result(success=True)
        except Exception as e:
            return api_result(str(e), success=False)

    @app.post("/api/chat/delete/{chat_id}")
    async def chat_delete(chat_id: str, request: fastapi.Request):
        """Deletes a chat by its ID"""
        uctx, username = await _get_uctx(request)
        if chat_id == _current_chat_id(uctx):
            await channel.websocket_manager.cancel_stream(username)
        await uctx.chat.delete(chat_id)
        return api_result(success=True)

    @app.post("/api/chat/category/{chat_id}")
    async def chat_set_category(chat_id: str, request: fastapi.Request):
        """Changes the category of a chat by its ID"""
        uctx, _ = await _get_uctx(request)
        try:
            data = await request.json()
            new_category = data.get('category', 'general').strip()
            if not new_category:
                return api_result("Category cannot be empty", success=False)

            index = uctx.chat._find_index(chat_id)
            if index is None:
                return api_result("Chat not found", success=False)

            await uctx.chat.set("category", new_category, index=index)

            return api_result(success=True)
        except Exception as e:
            return api_result(str(e), success=False)

    @app.post("/api/chat/meeting_room/{chat_id}")
    async def chat_set_meeting_room(chat_id: str, request: fastapi.Request):
        """Marks or unmarks a chat as a meeting room (multiple meetings in one chat)"""
        uctx, username = await _get_uctx(request)
        try:
            data = await request.json()
            enabled = bool(data.get("enabled"))

            index = uctx.chat._find_index(chat_id)
            if index is None:
                return api_result("Chat not found", success=False)

            uctx.chat.update_metadata("meeting_room", enabled, index=index)

            await channel.websocket_manager.broadcast({
                "type": "chat_metadata_updated",
                "id": uctx.chat.data[index].get("id"),
                "metadata": uctx.chat.data[index].get("metadata"),
            }, username=username)

            return api_result(success=True)
        except Exception as e:
            return api_result(str(e), success=False)

    @app.post("/api/chat/meeting/boundary/{chat_id}")
    async def chat_meeting_boundary(chat_id: str, request: fastapi.Request):
        """Appends a meeting boundary marker to a chat, isolating the new meeting's
        context from previous meetings (no-op on an empty chat)"""
        uctx, username = await _get_uctx(request)
        try:
            try:
                data = await request.json()
            except Exception:
                data = {}

            index = uctx.chat._find_index(chat_id)
            if index is None:
                return api_result("Chat not found", success=False)

            # messages.add always targets the currently loaded chat, so switch
            # to the target chat first and let the user's other tabs follow
            if uctx.chat.get("id") != chat_id:
                await uctx.chat.load(chat_id)
                await channel.websocket_manager.broadcast({
                    "type": "chat_switched", "id": chat_id
                }, username=username)

            history = await uctx.chat.messages.get()
            if not history:
                # first meeting in a fresh room needs no divider
                return api_result({"appended": False}, success=True)

            now = datetime.datetime.now()
            await uctx.chat.messages.add({
                "role": "user",
                "content": f"New meeting · {now.strftime('%Y-%m-%d %H:%M')}",
                "_metadata": {
                    "signal": "MEETING_CUTOFF",
                    "meeting": {
                        "startedAt": now.isoformat(),
                        "title": data.get("title") or "",
                    },
                },
            })

            return api_result({"appended": True}, success=True)
        except Exception as e:
            return api_result(str(e), success=False)

    # --- Settings
    # -- GET
    @app.get("/api/settings/load")
    async def settings_load(request: fastapi.Request):
        """Returns the core's config object as a json object, merged with per-user config."""
        username = request.session.get("username")
        global_config = dict(core.config.config)
        if username:
            merged = core.config._merge_user_config_over(global_config, username)
            return api_result(merged)

        # No username: still fill in defaults for sections migrated to per-user config.
        merged = dict(global_config)
        for k, v in core.config.default_config.items():
            if k not in merged:
                merged[k] = v
        return api_result(merged)

    @app.get("/api/settings/get_module_info")
    async def get_module_info():
        """Returns the schemas (descriptions, settings schemas, etc) for all modules and core config sections"""
        module_info = {}

        # Add module/channel settings schemas
        for module_name, module_data in core.config.get_module_structure().items():
            metadata = module_data.get("metadata", {})
            settings_schema = module_data.get("settings", {})

            if module_name not in module_info.keys():
                module_info[module_name] = {
                    "description": metadata.get("doc", ""),
                    "unsafe": metadata.get("unsafe", False),
                    "settings_schema": settings_schema
                }

        # Add core config sections settings schemas
        core_structure = core.config.get_core_settings_structure()
        for section_name, section_data in core_structure.items():
            if section_name not in module_info.keys():
                module_info[section_name] = {
                    "description": section_data.get("metadata", {}).get("doc", ""),
                    "unsafe": section_data.get("metadata", {}).get("unsafe", False),
                    "settings_schema": section_data.get("settings", {})
                }

        return api_result(module_info)

    @app.get("/api/check_connection")
    async def check_connection():
        """returns True if the backend is connected to the AI API, else False"""
        if channel.manager.API.connected:
            return api_result(True, success=True)
        else:
            return api_result("not connected", success=False)

    @app.get("/api/models")
    async def models_get():
        """Returns a list of all available AI models"""
        result = await channel.manager.API.list_models()
        if isinstance(result, core.api.APIError):
            return api_result(str(result), success=False)

        return api_result(result)

    @app.post("/api/voice/transcribe")
    async def voice_transcribe(request: fastapi.Request):
        """Transcribes recorded audio (16kHz mono WAV, in memory) using the configured STT engine, and returns the text."""
        body = await request.json()
        audio_b64 = body.get("audio_data", "")
        audio_format = body.get("format", "wav")
        purpose = body.get("purpose", "preview")

        if not audio_b64:
            return api_result("No audio data provided", success=False)

        # per-user voice settings: language + vocabulary hint
        language = core.config.get("api", "voice_language", default="en") or "auto"
        hotwords = core.config.get("api", "voice_hotwords", default="") or ""

        try:
            text = await core.stt.transcribe(
                base64.b64decode(audio_b64),
                purpose=purpose,
                language=language,
                prompt=hotwords or None,
                audio_format=audio_format
            )
        except core.stt.STTError as e:
            return api_result(str(e), success=False)
        except Exception as e:
            return api_result(f"Transcription failed: {core.detail_error(e)}", success=False)

        return api_result({"text": text}, success=True)

    # -- POST
    @app.post("/api/settings/save")
    async def settings_save(request: fastapi.Request):
        """Saves config data to the backend. Separates per-user settings from global settings."""
        data = await request.json()

        changed_modules = list(data.get("changed_modules", []))
        data.pop("changed_modules", None)

        username = request.session.get("username")

        if username:
            # Separate per-user from global settings
            per_user_data = {}
            global_data = {}

            for key, value in data.items():
                if key in core.config.PER_USER_KEYS:
                    per_user_data[key] = value
                elif key == "core":
                    for ck, cv in (value if isinstance(value, dict) else {}).items():
                        if ck in core.config.PER_USER_CORE_KEYS:
                            per_user_data.setdefault("core", {})[ck] = cv
                        else:
                            global_data.setdefault("core", {})[ck] = cv
                elif key == "modules" and isinstance(value, dict):
                    if "settings" in value:
                        per_user_data.setdefault("modules", {})["settings"] = value["settings"]
                    for mk, mv in value.items():
                        if mk != "settings":
                            global_data.setdefault("modules", {})[mk] = mv
                else:
                    global_data[key] = value

            if per_user_data:
                user_cfg = core.config.load_user_config(username)
                user_cfg = core.config._deep_merge(user_cfg, per_user_data)
                core.config.save_user_config(username, user_cfg)

            # only admins can persist global (instance-wide) settings;
            # for non-admins, global_data is dropped (UX gating is in the frontend too)
            if global_data and _require_admin(request):
                core.config.config.load(global_data)
                core.config.config.save()
        else:
            result = core.config.config.load(data=data)
            core.config.config.save()
            if not result:
                return api_result(success=False)

        # Reload modules that had their settings changed
        if changed_modules:
            for module_name in changed_modules:
                try:
                    await channel.manager.reload_module(module_name)
                except Exception as e:
                    channel.log(channel.name, f"Error reloading module {module_name}: {core.detail_error(e)}")

        return api_result(success=True)

    @app.post("/api/settings/detected_timezone")
    async def set_detected_timezone(request: fastapi.Request):
        """Stores the browser-detected IANA timezone for the current user (per-user).

        The time module consults this only when the user hasn't set an explicit
        timezone override, so the AI's "current time" reflects the user's zone
        instead of the server's. An empty string clears the stored value."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        tz = (body.get("timezone") or "").strip()

        # empty is allowed (clears detection); otherwise it must resolve to a
        # real zone. validated via ZoneInfo() rather than available_timezones()
        # so convenience names browsers report (e.g. "UTC") are accepted, and so
        # the whole zone database isn't re-scanned on every request
        if tz:
            try:
                zoneinfo.ZoneInfo(tz)
            except Exception:
                return api_result("invalid timezone", success=False)

        path = core.get_data_path("detected_timezone")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tz)

        return api_result(success=True)

    # --- User management (admin only)
    def _require_admin(request):
        """Check if user is admin, re-validating against user store."""
        username = request.session.get("username")
        if not username:
            return False
        user = channel.user_manager.get_user(username)
        if not user:
            return False
        return user.get("role") == "admin"

    def _system_authorize(request):
        """Gate for system-level endpoints (restart/reconnect/logs).
        multi-user mode (require_login) requires an admin; single-user mode
        has no role concept and keeps working as before"""
        if not channel.config.get("require_login", False):
            return True
        return _require_admin(request)

    def _verify_csrf(request):
        """Verify CSRF token using double submit cookie pattern."""
        session_csrf = request.session.get("_csrf")
        header_csrf = request.headers.get("x-csrf-token", "")
        if not session_csrf or not header_csrf:
            return False
        return secrets.compare_digest(session_csrf, header_csrf)

    @app.get("/api/users")
    async def api_list_users(request: fastapi.Request):
        """List all users (admin only)."""
        if not _require_admin(request):
            return api_result("Unauthorized", success=False)
        users = channel.user_manager.list_users()
        return api_result(users)

    @app.post("/api/users")
    async def api_create_user(request: fastapi.Request):
        """Create a new user (admin only)."""
        if not _require_admin(request):
            return api_result("Unauthorized", success=False)
        if not _verify_csrf(request):
            return api_result("CSRF token missing or invalid", success=False)
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        role = data.get("role", "user")

        if not username or not password:
            return api_result("Username and password are required", success=False)

        if channel.user_manager.get_user(username):
            return api_result("User already exists", success=False)

        result = channel.user_manager.create_user(username, password, role)
        if result:
            # give the new user a working per-user config by inheriting the
            # creating admin's api/model sections (without them the user would
            # be stuck on the placeholder connection defaults)
            admin_username = request.session.get("username")
            if admin_username and not core.config.load_user_config(username):
                admin_cfg = core.config.load_user_config(admin_username)
                inherited = {key: admin_cfg[key] for key in ("api", "model") if key in admin_cfg}
                if inherited:
                    core.config.save_user_config(username, inherited)

            return api_result({"username": username, "role": role})
        return api_result("Failed to create user", success=False)

    @app.patch("/api/users/{username}")
    async def api_update_user(username: str, request: fastapi.Request):
        """Update user role or password (admin only)."""
        if not _require_admin(request):
            return api_result("Unauthorized", success=False)
        if not _verify_csrf(request):
            return api_result("CSRF token missing or invalid", success=False)
        data = await request.json()

        if "role" in data:
            new_role = data["role"]
            if new_role not in ("admin", "user"):
                return api_result("Invalid role", success=False)
            if not channel.user_manager.update_role(username, new_role):
                return api_result("Failed to update role", success=False)

        if "password" in data and data["password"]:
            if not channel.user_manager.change_password(username, data["password"]):
                return api_result("Failed to change password", success=False)

        return api_result(channel.user_manager.get_user(username))

    @app.delete("/api/users/{username}")
    async def api_delete_user(username: str, request: fastapi.Request):
        """Delete a user (admin only)."""
        if not _require_admin(request):
            return api_result("Unauthorized", success=False)
        if not _verify_csrf(request):
            return api_result("CSRF token missing or invalid", success=False)

        current_user = request.session.get("username")
        if current_user == username:
            return api_result("Cannot delete yourself", success=False)

        # quiesce the user before their data folder goes: disconnect them so no
        # new message can start mid-deletion, stop their stream, and write their
        # chat's pending (debounced) saves now rather than into a missing folder
        # later (written, not dropped: if the deletion fails, nothing is lost)
        await channel.websocket_manager.close_where(username=username, code=4001, reason="User deleted")
        await channel.websocket_manager.cancel_stream(username)
        user_context = channel.user_contexts.get(username)
        if user_context is not None:
            await user_context.chat.flush_pending_saves()

        # then let modules stop per-user background work and close per-user files
        # (eg. memory's sqlite database): on windows an open file makes the
        # folder deletion fail halfway, after the account itself is already gone
        for module in channel.manager.modules.values():
            try:
                await module.on_before_user_delete(username)
            except Exception as e:
                channel.log("error", f"{module.name}: error in on_before_user_delete(): {core.detail_error(e)}")

        # delete_user() is synchronous, so nothing can run between the hooks above and the deletion
        if not channel.user_manager.delete_user(username):
            return api_result("Failed to delete user", success=False)

        # evict the cached per-user context so that a user recreated with the
        # same name can't inherit the deleted user's in-memory chat state (whose
        # storage paths point at the deleted data dir)
        channel.user_contexts.pop(username, None)

        # drop each module's per-user in-memory state (per-user storage caches)
        for module in channel.manager.modules.values():
            try:
                await module.on_user_deleted(username)
            except Exception as e:
                channel.log("error", f"{module.name}: error in on_user_deleted(): {core.detail_error(e)}")
        # again: a client may have reconnected while the steps above were running
        await channel.websocket_manager.close_where(username=username, code=4001, reason="User deleted")

        return api_result(success=True)

    @app.post("/api/reconnect")
    async def reconnect(request: fastapi.Request):
        """Disconnects and then reconnects the API (admin only in multi-user mode)."""
        if not _system_authorize(request):
            return api_result("Unauthorized", success=False)
        result = await channel.manager.API.reconnect()
        if isinstance(result, core.api.APIError):
            return api_result(str(result), success=False)

        return api_result(success=True)

    # ----------------------------
    # Artifact (live site preview) API
    # ----------------------------
    # site files are served under /api/artifact/{token}/..., where the token is
    # signed and names the user + one site folder. the session cookie can't be
    # used: the preview iframe has an opaque origin (see the CSP below), so the
    # page's own requests for its images/css/pages are cross-site and the
    # SameSite=Lax cookie is never sent. relative urls inside the site keep the
    # token prefix for free. scoping it to the site means a hostile page that
    # leaks its own url exposes nothing beyond itself.
    artifact_signer = itsdangerous.URLSafeTimedSerializer(channel._get_session_secret(), salt="openlumara-artifact")

    @app.get("/api/artifact-token")
    async def artifact_token(request: fastapi.Request, sandbox: str, folder: str = ""):
        """issues the url token for one site (session-authenticated like every other /api route)"""
        username = request.session.get("username") if channel.config.get("require_login", False) else None
        folder = folder.replace("\\", "/").strip("/")
        return api_result({"token": artifact_signer.dumps({"u": username or "", "s": sandbox, "f": folder})})

    @app.get("/api/artifact/{token}/{sandbox}/{path:path}")
    async def artifact_file(token: str, sandbox: str, path: str):
        """serves a file from a coder sandbox so the in-app preview iframe can render it"""
        not_found = fastapi.responses.JSONResponse({"detail": "not found"}, status_code=404)
        coder = channel.manager.modules.get("coder")
        if not coder:
            return fastapi.responses.JSONResponse({"detail": "coder module not available"}, status_code=503)

        try:
            claims = artifact_signer.loads(token, max_age=channel.config.get("login_lifetime") * 86400)
        except itsdangerous.BadData:
            return not_found

        parts = path.replace("\\", "/").split("/")
        folder = claims.get("f", "")
        if claims.get("s") != sandbox or ".." in parts or (folder and not path.startswith(f"{folder}/")):
            return not_found

        username = claims.get("u") or None
        if channel.config.get("require_login", False):
            # a deleted account's tokens die with it
            if not username or not channel.user_manager.get_user(username):
                return not_found

        # this route skips the session middleware, so resolve the sandbox as the token's user
        user_token = core.current_user.set(username)
        try:
            full = await coder._get_sandbox_subpath(sandbox, path)   # already path-safe via core.sandbox_path
        except Exception:
            return not_found
        finally:
            core.current_user.reset(user_token)

        if not os.path.isfile(full):
            return fastapi.responses.JSONResponse({"detail": "not found"}, status_code=404)

        media = fastapi.responses.FileResponse(full)
        media.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        media.headers["Pragma"] = "no-cache"
        # served same-origin, so a hostile page the AI builds (e.g. from
        # prompt-injected content) could otherwise read the parent DOM, the
        # session cookie, and call the app API. the CSP sandbox gives the
        # document an opaque origin; allow-scripts/allow-modals keep normal
        # sites functional
        media.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-modals"
        return media

    # ----------------------------
    # Site media API (the preview panel's media tab)
    # sites are addressed by sandbox + the folder holding their entry html.
    # everything resolves through the coder, so it's path-safe and per-user.
    # ----------------------------
    def _media_guard(request):
        """returns (coder, error_response)"""
        coder = channel.manager.modules.get("coder")
        if not coder:
            return None, api_result("coder module not available", success=False)
        if request.method != "GET" and channel.config.get("require_login", False) and not _verify_csrf(request):
            return None, api_result("CSRF token missing or invalid", success=False)
        return coder, None

    async def _read_upload(coder, upload):
        """reads an UploadFile, stopping one byte past the size cap instead of buffering an unbounded body"""
        max_bytes = (coder.config.get("max_media_size_mb") or 15) * 1024 * 1024
        data = await upload.read(max_bytes + 1)
        coder._check_media_size(data)
        return data

    @app.get("/api/site-media")
    async def site_media_list(request: fastapi.Request, sandbox: str, folder: str = ""):
        coder, error = _media_guard(request)
        if error:
            return error
        try:
            return api_result(await coder._list_site_media(sandbox, folder))
        except Exception as e:
            return api_result(str(e), success=False)

    @app.post("/api/site-media/upload")
    async def site_media_upload(request: fastapi.Request):
        coder, error = _media_guard(request)
        if error:
            return error

        form = await request.form()
        sandbox = form.get("sandbox") or ""
        folder = form.get("folder") or ""

        saved, errors = [], []
        try:
            for upload in form.getlist("files"):
                if not hasattr(upload, "read"):
                    continue
                try:
                    data = await _read_upload(coder, upload)
                    saved.append(await coder._save_site_media(sandbox, folder, upload.filename, data))
                except Exception as e:
                    errors.append(f"{upload.filename}: {e}")
        finally:
            await form.close()

        if errors and not saved:
            return api_result("\n".join(errors), success=False)
        return api_result({"saved": saved, "errors": errors})

    @app.post("/api/site-media/replace")
    async def site_media_replace(request: fastapi.Request):
        coder, error = _media_guard(request)
        if error:
            return error

        form = await request.form()
        try:
            upload = form.get("file")
            if not hasattr(upload, "read"):
                return api_result("No file was uploaded", success=False)
            data = await _read_upload(coder, upload)
            result = await coder._replace_site_placeholder(
                form.get("sandbox") or "", form.get("folder") or "", form.get("placeholder") or "",
                upload.filename, data)
        except Exception as e:
            return api_result(str(e), success=False)
        finally:
            await form.close()
        return api_result(result)

    @app.post("/api/site-media/import")
    async def site_media_import(request: fastapi.Request):
        coder, error = _media_guard(request)
        if error:
            return error

        body = await request.json()
        try:
            result = await coder._import_site_media(
                body.get("sandbox") or "", body.get("folder") or "", body.get("url") or "")
        except Exception as e:
            return api_result(str(e), success=False)
        return api_result(result)

    @app.post("/api/site-media/delete")
    async def site_media_delete(request: fastapi.Request):
        coder, error = _media_guard(request)
        if error:
            return error

        body = await request.json()
        try:
            await coder._delete_site_media(body.get("sandbox") or "", body.get("folder") or "", body.get("name") or "")
        except Exception as e:
            return api_result(str(e), success=False)
        return api_result(success=True)

    # ----------------------------
    # System.. stuff
    # ----------------------------
    # -- GET
    @app.get("/api/system/data")
    async def get_data():
        """returns any relevant data for the webUI to use"""
        data = {
            "max_context": core.config.get("api", "max_context")
        }

        return api_result(data)
    @app.get("/api/system/logs")
    async def get_logs(request: fastapi.Request):
        """returns the channel log history (admin only in multi-user mode,
        since logs may contain other users' content)"""
        if not _system_authorize(request):
            return api_result("Unauthorized", success=False)
        return api_result(list(channel.logs))

    # -- POST
    @app.post("/api/system/restart")
    async def restart_server(request: fastapi.Request):
        """restarts the server (admin only in multi-user mode)"""
        if not _system_authorize(request):
            return api_result("Unauthorized", success=False)
        await channel.manager.restart()

    # ----------------------------
    # Theme API endpoints
    # ----------------------------
    @app.get("/api/themes")
    async def get_themes():
        """Returns a list of available theme families with their supported modes (dark/light)"""
        themes_dir = os.path.join(channel.path, "themes")
        theme_list = []

        for f in os.listdir(themes_dir):
            if f.endswith('.json') and f != 'base.json':
                family_name = f[:-5]
                filepath = os.path.join(themes_dir, f)
                try:
                    with open(filepath, 'r', encoding='utf-8') as fh:
                        theme_data = json.load(fh)
                        theme_list.append({
                            "name": family_name,
                            "dark": "dark" in theme_data,
                            "light": "light" in theme_data
                        })
                except Exception as e:
                    channel.log(channel.name, f"failed to read theme {filepath}: {e}")

        theme_list.sort(key=lambda x: x["name"])
        return theme_list

    @app.get("/api/themes/{family_name}")
    async def get_theme(family_name: str):
        """Returns full theme data for a specific family"""
        themes_dir = os.path.join(channel.path, "themes")
        filepath = os.path.join(themes_dir, f"{family_name}.json")

        if not os.path.exists(filepath):
            return api_result(f"Theme family '{family_name}' not found", success=False)

        try:
            with open(filepath, 'r', encoding='utf-8') as fh:
                theme_data = json.load(fh)
            return theme_data
        except Exception as e:
            channel.log(channel.name, f"failed to load theme {filepath}: {e}")
            return api_result(f"Failed to load theme: {str(e)}", success=False)

    @app.get('/sw.js')
    async def service_worker():
        sw_template_path = os.path.join(channel.path, 'sw.js')
        with open(sw_template_path) as f:
            sw_code = f.read()

        version = channel._sw_version
        file_list = ',\n    '.join(f'"{f}"' for f in channel._sw_assets)
        sw_code = sw_code.replace('{{VERSION}}', version)
        sw_code = sw_code.replace('{{FILE_LIST}}', f'{file_list}\n')

        return fastapi.Response(
            content=sw_code,
            media_type='application/javascript',
            headers={
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0',
            }
        )

    @app.get('/manifest.json')
    async def manifest():
        """Serve the PWA manifest."""
        with open(os.path.join(channel.path, "manifest.json")) as f:
            manifest_data = json.loads(f.read())
        return manifest_data

    @app.get('/icon-192.png')
    async def icon_192():
        """Serve the 192x192 icon for PWA."""
        return fastapi.responses.FileResponse(os.path.join(channel.path, "icon-192.png"))

    @app.get('/icon-512.png')
    async def icon_512():
        """Serve the 512x512 icon for PWA."""
        return fastapi.responses.FileResponse(os.path.join(channel.path, "icon-512.png"))

    @app.get('/favicon.ico')
    async def favicon():
        """Serve the favicon for the web interface."""
        return fastapi.responses.FileResponse(os.path.join(channel.path, "favicon.ico"))

    # ------------------
    # WebSocket endpoint
    # ------------------
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: fastapi.WebSocket):
        # cross-origin guard: must run before accept(), browsers do not apply
        # CORS to WebSockets, so this is the only thing standing between a
        # hostile page and driving the agent over ws://localhost:3000/ws
        if not channel.websocket_origin_allowed(websocket):
            origin = websocket.headers.get("origin", "<missing>")
            channel.log(channel.name, f"Rejected WebSocket handshake from origin {origin}")
            await websocket.close(code=4003, reason="Cross-origin WebSocket rejected")
            return

        # WebSocket auth check: trust only the signed session contents.
        # SessionMiddleware leaves scope["session"] empty for a missing, forged
        # or expired cookie, so the mere presence of a "session" cookie proves
        # nothing (any client could send session=x and land on the global context)
        ws_username = None
        ws_sid = None
        if channel.config.get("require_login", False):
            session = websocket.scope.get("session") or {}
            if session.get("authenticated"):
                ws_username = session.get("username")
                # per-login random token: lets /logout close this session's sockets
                ws_sid = session.get("_csrf")
            if not ws_username:
                # check if rate limited
                client_ip = websocket.client.host if websocket.client else "unknown"
                now = time.time()
                reason = "Unauthorized"

                if client_ip in channel.login_attempts:
                    channel.login_attempts[client_ip] = [
                        t for t in channel.login_attempts[client_ip] if now - t < 900
                    ]
                    if len(channel.login_attempts[client_ip]) >= 5:
                        reason = "Rate limited"

                # closing before accept() turns into an HTTP 403 on the handshake,
                # which the browser reports as a bare 1006 - then the client can't
                # tell "logged out" from "network down" and retries forever.
                # accept first so it receives 4001 and goes to the login page
                await websocket.accept()
                await websocket.close(code=4001, reason=reason)
                return

        ws_mgr = channel.websocket_manager

        try:
            await ws_mgr.connect(websocket, username=ws_username, sid=ws_sid)

            while True:
                data_text = await websocket.receive_text()

                try:
                    data = json.loads(data_text)
                    msg_type = data.get("type")

                    # heartbeat: the client can't see protocol-level pings, so it
                    # uses this to spot a half-open socket (e.g. after iOS
                    # resumes a backgrounded page) and reconnect
                    if msg_type == "ping":
                        ws_mgr.send(websocket, {"type": "pong", "id": data.get("id")})
                        continue

                    # the client's copy of the streaming turn no longer lines up
                    # with a turn_delta offset: resend the whole snapshot
                    if msg_type == "resync":
                        ws_mgr.send_resume(websocket, ws_username)
                        continue

                    # Set user context for per-user data isolation
                    if ws_username:
                        core.current_user.set(ws_username)

                    # Get per-user context
                    uctx = await channel._get_user_context(ws_username) if ws_username else channel.context

                    match msg_type:
                        case "stop":
                            # stop only THIS user's own stream (per-user cancel token).
                            # the old global API.cancel() would also cancel/collide
                            # with other users' in-flight streams
                            stream_entry = ws_mgr.streams.get(ws_username or None)
                            if stream_entry and not stream_entry["cancel"].is_set():
                                stream_entry["cancel"].set()
                        case "reload_messages":
                            # only the user who requested the reload should resync
                            await ws_mgr.broadcast({
                                "type": "sync"
                            }, username=ws_username)
                        case "rename":
                            new_title = data.get("title")
                            if channel and new_title:
                                await uctx.chat.set("title", new_title)
                                await ws_mgr.broadcast({
                                    "type": "chat_metadata_updated",
                                    "id": uctx.chat.get("id"),
                                    "title": new_title,
                                    "tags": uctx.chat.get("tags") or []
                                }, username=ws_username)
                        case "switch_chat":
                            new_chat_id = data.get("chat_id")
                            if new_chat_id:
                                # cancel only this user's own in-flight stream
                                await ws_mgr.cancel_stream(ws_username)

                                try:
                                    await uctx.chat.load(new_chat_id)
                                except Exception as e:
                                    await ws_mgr.broadcast({"type": "error", "content": f"Failed to load chat: {e}"}, username=ws_username)

                                await ws_mgr.broadcast({
                                    "type": "chat_switched",
                                    "id": new_chat_id,
                                }, username=ws_username)
                        case "new_chat":
                            # cancel only this user's own in-flight stream
                            await ws_mgr.cancel_stream(ws_username)

                            new_id = await uctx.chat.new()

                            await ws_mgr.broadcast({
                                "type": "chat_switched",
                                "id": new_id,
                                "buffer": []
                            }, username=ws_username)
                        case "chat_delete":
                            chat_id = data.get("chat_id")
                            if not chat_id:
                                continue

                            # the stream may be writing into the chat being deleted
                            if chat_id == _current_chat_id(uctx):
                                await ws_mgr.cancel_stream(ws_username)
                            await uctx.chat.delete(chat_id)
                            await ws_mgr.broadcast({
                                "type": "chat_switched",
                                "id": uctx.chat.get("id"),
                                "buffer": []
                            }, username=ws_username)
                        case "user_message":
                            text = data.get("content")
                            files_data = data.get("files")
                            target_chat_id = data.get("chat_id")

                            if not text and not files_data:
                                continue

                            files_list = None
                            if files_data:
                                size_error = upload_size_error(files_data)
                                if size_error:
                                    await ws_mgr.broadcast({
                                        "type": "error",
                                        "error": size_error
                                    }, username=ws_username)
                                    continue

                                files_list = await decode_upload_files(files_data)

                            # finish off a running stream before touching the chat:
                            # loading the target chat first would let the old
                            # stream's partial reply land in it
                            await ws_mgr.cancel_stream(ws_username)

                            chat_id = uctx.chat.get("id") or "default"

                            # explicit chat targeting (meeting transcripts must land
                            # in the meeting chat even if the user navigated away).
                            # the UI follows the transcript via chat_switched.
                            if target_chat_id and target_chat_id != chat_id:
                                try:
                                    await uctx.chat.load(target_chat_id)
                                except Exception as e:
                                    await ws_mgr.broadcast({
                                        "type": "error",
                                        "error": f"Failed to load chat: {e}"
                                    }, username=ws_username)
                                    continue

                                await ws_mgr.broadcast({
                                    "type": "chat_switched",
                                    "id": target_chat_id,
                                }, username=ws_username)
                                chat_id = target_chat_id

                            # the client names the site open in the preview panel; its
                            # media attachments get saved there. the note rides along in
                            # metadata so it reaches the AI without showing in the bubble
                            message = text
                            attachment_note = await save_attachments_to_site(
                                channel.manager.modules.get("coder"), data.get("site"), files_list)
                            if attachment_note:
                                message = {"content": text or "", "_metadata": {"attachment_note": attachment_note}}

                            await ws_mgr.start_stream(channel, chat_id, message=message, files=files_list, ws_username=ws_username)
                        case "message_edit":
                            index = data.get("index")
                            if index is None or index < 0:
                                continue

                            message = await uctx.chat.messages.get(index)
                            content = data.get("content")
                            filenames = data.get("filenames")

                            # new files attached during edit (same format as user_message)
                            files_data = data.get("files")
                            if files_data:
                                size_error = upload_size_error(files_data)
                                if size_error:
                                    await ws_mgr.broadcast({
                                        "type": "error",
                                        "error": size_error
                                    }, username=ws_username)
                                    continue

                                files_list = await decode_upload_files(files_data)

                                # if the message had plain string content, convert it to blocks
                                # so file blocks can be appended
                                if isinstance(content, str):
                                    content = [{"type": "text", "text": content}]
                                    filenames = [""]

                                # keep filenames aligned with content if not provided
                                if filenames is None:
                                    filenames = ["" for _ in content]

                                for filename, file_data in files_list:
                                    block = channel._file_to_block(filename, file_data)
                                    if block is None:
                                        continue
                                    content.append(block)
                                    filenames.append(filename)

                            message["content"] = content

                            if filenames is not None:
                                message.setdefault("_metadata", {})["filenames"] = filenames

                            await uctx.chat.messages.edit(index, message)

                            await ws_mgr.broadcast({
                                "type": "sync"
                            }, username=ws_username)
                        case "message_delete":
                            index = data.get("index")
                            if index is None or index < 0:
                                continue

                            # a running stream would append after the cut
                            await ws_mgr.cancel_stream(ws_username)
                            await uctx.chat.messages.delete_from(index)
                            await ws_mgr.broadcast({
                                "type": "sync"
                            }, username=ws_username)
                        case "message_regenerate":
                            index = data.get("index")

                            if index is not None and channel:
                                # stop the current stream before truncating the
                                # history it is still writing to
                                await ws_mgr.cancel_stream(ws_username)

                                last_user_message_index = await uctx.chat.messages.get_last_message_with_role("user", cutoff_index=index)

                                if last_user_message_index == -1:
                                    await ws_mgr.broadcast({
                                        "type": "error",
                                        "error": "Could not regenerate message (no preceding user message found)"
                                    }, username=ws_username)
                                    continue

                                user_message = await uctx.chat.messages.get(last_user_message_index)

                                # delete_from deletes all messages AFTER the target, so we need to do index-1
                                # max(0, index) clamps it so that it never goes below 0
                                await uctx.chat.messages.delete_from(max(0, last_user_message_index))

                                await ws_mgr.broadcast({"type": "sync"}, username=ws_username)
                                # pass the full message so _metadata survives re-sending
                                await ws_mgr.start_stream(channel, uctx.chat.get("id"), user_message, ws_username=ws_username)
                        case _:
                            channel.log(channel.name, f"Unknown websocket command received: {msg_type}")

                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    channel.log(channel.name, f"WebSocket command error: {core.detail_error(e)}")

        except fastapi.WebSocketDisconnect:
            pass
        except Exception as e:
            channel.log(channel.name, f"WebSocket error: {core.detail_error(e)}")
        finally:
            # also runs on cancellation (shutdown), so no connection is ever leaked
            ws_mgr.disconnect(websocket)

    return app

# -------------------
# Websocket Manager
# -------------------
def _utf16_len(text):
    # offsets are checked against JS string lengths, which count UTF-16 units
    return len(text.encode("utf-16-le")) // 2


def _same_slot(a, b):
    """whether segment b updates segment a in place (the frontend's grouping rule)"""
    return a.get("type") == b.get("type") and a.get("tool_call_id") == b.get("tool_call_id")


class _TurnSender:
    """coalesces a stream's turn updates before they hit the socket.

    group_stream yields the whole accumulated segment on every token, so
    forwarding each one costs O(n^2) bytes over a reply (megabytes for a long
    answer, which backs up slow mobile connections until the ping timeout
    drops them). instead:
      - updates are sent at most every FLUSH_INTERVAL; a segment change or
        the end of the stream always flushes first, so nothing is lost
      - growing text segments go out as turn_delta appends with the UTF-16
        offset they apply at, so a client can drop a duplicate (e.g. after a
        stream_resume snapshot raced ahead of it) or ask for a resync
    entry["turn"] tracks exactly what was broadcast, which is what
    stream_resume hands out and what the next delta is relative to.
    """

    FLUSH_INTERVAL = 0.05
    TEXT_KEYS = {"content": "content", "reasoning": "reasoning_content"}

    def __init__(self, manager, entry, username):
        self.manager = manager
        self.entry = entry
        self.username = username
        self.pending = None
        self.timer = None

    async def update(self, segment):
        if self.pending is not None and not _same_slot(self.pending, segment):
            await self.flush()
        self.pending = segment
        if self.timer is None or self.timer.done():
            self.timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self):
        # keep going while updates land during a flush, so the latest one is
        # never stranded until the next token (which may be a while away)
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            await self.flush()
            if self.pending is None:
                return

    async def flush(self):
        segment, self.pending = self.pending, None
        if segment is None:
            return

        messages = self.entry["turn"]["messages"]
        last = messages[-1] if messages else None
        key = self.TEXT_KEYS.get(segment.get("type"))
        old = (last.get(key) or "") if key and last else ""
        new = (segment.get(key) or "") if key else ""

        if key and last and _same_slot(last, segment) and new.startswith(old):
            messages[-1] = segment
            if new == old:
                return
            payload = {"type": "turn_delta", "key": key, "offset": _utf16_len(old), "append": new[len(old):]}
        else:
            WebSocketManager._merge_segment(self.entry["turn"], segment)
            payload = {"type": "turn_stream", "turn": segment}

        await self.manager.broadcast(payload, username=self.username)

    async def close(self):
        if self.timer and not self.timer.done():
            self.timer.cancel()
        await self.flush()


class WebSocketManager:
    # every connection gets its own outbox drained by its own writer task, so a
    # slow or half-dead client (a phone on bad signal) only ever stalls itself.
    # broadcasting used to await each send in turn, so one stuck socket froze
    # the stream for that user's other tabs and every admin log broadcast
    OUTBOX_SIZE = 1000
    SEND_TIMEOUT = 30

    def __init__(self, channel):
        self.channel = channel

        # (websocket, username) pairs
        self.active_connections = []

        # websocket -> {"queue", "task", "sid"} (sid identifies the login session)
        self.outboxes = {}

        # one stream task per connected user (keyed by username, or None for anonymous)
        self.streams = {}
        self.roles = {}

    async def connect(self, websocket: fastapi.WebSocket, username=None, sid=None):
        await websocket.accept()

        queue = asyncio.Queue(maxsize=self.OUTBOX_SIZE)
        self.outboxes[websocket] = {
            "queue": queue,
            "task": asyncio.create_task(self._writer(websocket, queue)),
            "sid": sid,
        }
        self.active_connections.append((websocket, username))

        if username:
            try:
                self.roles[username] = self.channel.user_manager.get_user(username).get("role")
            except Exception:
                self.roles.pop(username, None)

        # always signal ready on connect so the client can load data
        # (the frontend ignores it if a chat isn't selected yet)
        self.send(websocket, {
            "type": "ready"
        })

        # a stream outlives its connections (phones drop the socket whenever the
        # page is backgrounded), so hand a reconnecting tab everything streamed so far
        self.send_resume(websocket, username)

    def send_resume(self, websocket, username):
        """send the user's running stream (if any) as a snapshot that later
        turn_stream/turn_delta updates continue on top of"""
        entry = self.streams.get(username)
        if entry and not entry["task"].done():
            self.send(websocket, {
                "type": "stream_resume",
                "turn": entry["turn"],
                "state": entry["state"],
                "index": entry["index"],
            })

    def send(self, websocket, message: dict):
        """queue a message for one connection (in order with its broadcasts)"""
        outbox = self.outboxes.get(websocket)
        if outbox is None:
            return
        try:
            outbox["queue"].put_nowait(message)
        except asyncio.QueueFull:
            # it isn't reading: drop it, the client reconnects and resumes
            self.channel.log(self.channel.name, "Dropping a websocket client that stopped reading")
            asyncio.ensure_future(self.close(websocket, code=1013))

    async def _writer(self, websocket, queue):
        while True:
            message = await queue.get()
            try:
                await asyncio.wait_for(websocket.send_json(message), timeout=self.SEND_TIMEOUT)
            except Exception:
                # gone (or stuck past the timeout); the endpoint's receive loop
                # notices too and finishes the cleanup
                self.disconnect(websocket)
                return
            finally:
                queue.task_done()

    def disconnect(self, websocket: fastapi.WebSocket):
        username = None
        for ws, u in self.active_connections:
            if ws is websocket:
                username = u
                break

        self.active_connections = [
            (ws, u) for ws, u in self.active_connections if ws is not websocket
        ]

        outbox = self.outboxes.pop(websocket, None)
        if outbox and outbox["task"] is not asyncio.current_task():
            outbox["task"].cancel()

        # the role is per user, not per tab: only forget it with the last connection
        if username and not any(u == username for _, u in self.active_connections):
            self.roles.pop(username, None)

        # the user's stream (if any) deliberately keeps running: it finishes and
        # saves the full reply, and a reconnecting tab picks it up through
        # stream_resume. only stop, a chat switch or a new message cancel it

    async def close(self, websocket, code=1000, reason=None):
        self.disconnect(websocket)
        try:
            await asyncio.wait_for(websocket.close(code=code, reason=reason), timeout=2)
        except Exception:
            pass

    async def close_where(self, username=None, sid=None, code=1000, reason=None):
        """close every connection of a user, or of one login session"""
        targets = [
            ws for ws, u in self.active_connections
            if (username is not None and u == username)
            or (sid is not None and self.outboxes.get(ws, {}).get("sid") == sid)
        ]
        for ws in targets:
            await self.close(ws, code=code, reason=reason)

    def broadcast_nowait(self, message: dict, username=None, admins_only=False):
        for ws, conn_username in list(self.active_connections):
            if username and conn_username != username:
                continue
            if admins_only and self.roles.get(conn_username) != "admin":
                continue
            self.send(ws, message)

    async def broadcast(self, message: dict, username=None, admins_only=False):
        self.broadcast_nowait(message, username=username, admins_only=admins_only)

    async def drain(self, timeout=1.0):
        """wait (briefly) for queued messages to go out, e.g. before shutdown"""
        joins = [asyncio.ensure_future(o["queue"].join()) for o in self.outboxes.values()]
        if joins:
            await asyncio.wait(joins, timeout=timeout)
            for j in joins:
                j.cancel()

    async def cancel_stream(self, username=None):
        entry = self.streams.pop(username, None)
        if entry and not entry["task"].done():
            entry["cancel"].set()
            entry["task"].cancel()
            # wait for its cleanup: the cancelled stream still saves its partial
            # reply into whatever chat is loaded when it gets there, and sends
            # stream_complete. left running, it would write into the chat the
            # caller loads next and finalize the client mid-way through the
            # next stream
            await asyncio.wait({entry["task"]}, timeout=5)

    # stream state a reconnecting client resumes in, by raw token type
    # (mirrors what the frontend derives from the same tokens)
    RESUME_STATES = {
        "reasoning": "thinking",
        "content": "streaming",
        "tool_call_delta": "calling_tools",
        "tool_calls": "calling_tools",
        "tool": "processing_tools",
        "prompt_progress": "processing",
    }

    @staticmethod
    def _merge_segment(turn, segment):
        """apply one turn_stream segment to a turn snapshot, the same way the frontend does"""
        messages = turn["messages"]
        last = messages[-1] if messages else None
        if last and last.get("type") == segment.get("type") and last.get("tool_call_id") == segment.get("tool_call_id"):
            messages[-1] = segment
        else:
            messages.append(segment)

    async def _stream_task(self, message: str, index, files: list = None, ws_username=None,
                           cancel_token=None, entry=None):
        user_message_confirmed = False
        sender = _TurnSender(self, entry, ws_username)

        try:
            async for partial in self.channel.turncollector.group_stream(
                    self.channel.send_stream(
                        message=message,
                        files=files,
                        commands_authorized=self.channel._commands_authorized_for(ws_username),
                        cancel_token=cancel_token
                    )
                ):

                if partial.get("type") == "token":
                    token = partial.get("content")
                    token_type = token.get("type")
                    match token_type:
                        case "user_message":
                            try:
                                user_msg_payload = token.copy()
                                user_msg_payload['index'] = index

                                # the client already has the files locally, and the
                                # full blocks come back via reloadChat() anyway -
                                # echoing multi-megabyte base64 blocks back over the
                                # socket just doubles bandwidth and stalls the loop
                                content = user_msg_payload.get("content")
                                if isinstance(content, list):
                                    user_msg_payload["content"] = [
                                        b for b in content
                                        if not (isinstance(b, dict) and b.get("type") in ("image_url", "input_audio"))
                                    ]

                                await self.broadcast({
                                    "type": "user_message_added",
                                    "message": user_msg_payload,
                                }, username=ws_username)
                            except Exception as e:
                                self.channel.log(self.channel.name, f"error sending user message: {core.detail_error(e)}")
                                return
                        case "error":
                            # for an error, just force a chat reload so that it shows up (core/channel takes care of adding it to context)
                            await self.broadcast({
                                "type": "user_message_confirmed",
                                "index": index
                            }, username=ws_username)
                            await self.broadcast({
                                "type": "sync"
                            }, username=ws_username)
                            return
                        case _:
                            if not user_message_confirmed:
                                user_message_confirmed = True
                                entry["state"] = "message_received"
                                await self.broadcast({
                                    "type": "user_message_confirmed",
                                    "index": index
                                }, username=ws_username)

                            previous_state = entry["state"]
                            entry["state"] = self.RESUME_STATES.get(token_type, entry["state"])

                            # content/reasoning text reaches the client through the
                            # turn updates, and repeated tool_call_deltas only re-set
                            # the same state, so don't echo those per token
                            if token_type in ("content", "reasoning"):
                                continue
                            if token_type == "tool_call_delta" and previous_state == "calling_tools":
                                continue

                            # keep the client's view of the turn ahead of state changes
                            await sender.flush()
                            await self.broadcast({
                                "type": "token",
                                "content": token
                            }, username=ws_username)

                elif partial.get("type") == "turn":
                    # group_stream re-yields (and keeps mutating) the same segment
                    # dict, so queue a copy of it as it is right now
                    await sender.update(dict(partial.get("content")))
        finally:
            await sender.close()

            # a finished stream must not be resumed by the next connection
            if self.streams.get(ws_username) is entry:
                self.streams.pop(ws_username, None)

            # always finalize the stream, no matter what
            await self.broadcast({
                "type": "stream_complete"
            }, username=ws_username)

    async def start_stream(self, channel, chat_id: str, message: str, files: list = None, ws_username=None):
        # only cancel this user's existing stream, so other users can stream concurrently
        await self.cancel_stream(ws_username)

        if ws_username:
            uctx = await channel._get_user_context(ws_username)
            next_index = len(await uctx.chat.messages.get())
        else:
            uctx = channel.context
            next_index = len(await uctx.chat.messages.get())

        # keep the cancel token on the user's context so in-stream commands
        # (e.g. /stop) can stop THIS user's stream without hitting the global
        # API.cancel() that would cancel every other user's stream
        token = asyncio.Event()
        uctx.cancel_token = token

        # turn/state/index are what stream_resume hands a reconnecting client
        entry = {
            "cancel": token,
            "turn": {"role": "assistant", "messages": []},
            "state": "message_sending",
            "index": next_index,
        }
        try:
            entry["task"] = asyncio.create_task(self._stream_task(
                message, next_index, files=files, ws_username=ws_username, cancel_token=token, entry=entry))
            self.streams[ws_username] = entry
        except Exception as e:
            channel.log(channel.name, f"Background stream error: {core.detail_error(e)}")

