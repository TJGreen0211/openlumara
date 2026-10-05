# AGENTS.md

OpenLumara: local-first, modular Python AI agent framework. Single app, no build
step, no monorepo, no CI. Entrypoint: `main.py`.

## Running

- `run.bat` / `run.sh` (or `venv\Scripts\python main.py` / `venv/bin/python main.py`).
  First run creates the venv and installs `requirements.txt`.
- WebUI: `http://127.0.0.1:3000` (port from `channels.settings.webui.port`), login
  on in the local config (admin/admin). Use the `.kilo/skills/webui-verify` skill for
  browser QA — it handles this machine's Windows/playwright-cli hang quirks.
- Useful flags: `--cli` (CLI only), `--tmp` (no data persistence), `--pure` (no modules),
  `--quiet`.
- `update.sh` / `update.bat` are end-user auto-updaters, not dev commands.

## Config gotchas

- `config.yml` is gitignored and **auto-synced by the framework on every startup**
  (adds missing schema keys, prunes removed ones, writes it back). Define new
  settings defaults in code (the `settings` dict of the module/channel class, or
  `core_settings_schema` in `core/config.py`), never by hand-editing `config.yml`.
- Never commit `config.yml`, `data/`, or `user_modules/` / `user_channels/` —
  local config holds real API URLs/keys.
- Per-user data (WebUI multi-user mode): `data/{username}/`; global: `data/`.
  Use `self.user_storage(name, type, cache, storage_cls)` from modules so data is
  per-user, not `core.storage.StorageDict` directly.

## Modules & channels (auto-discovery)

- Drop a `.py` file into `modules/`, `channels/`, `user_modules/`, or `user_channels/`
  with a class subclassing `core.module.Module` / `core.channel.Channel`. No
  registration, no imports elsewhere — loaded by pkgutil scan at startup.
- **The class name must exactly map to the snake_case filename** (e.g.
  `ExampleModule` in `example_module.py`). Enabled/disabled lists in config track
  file names; settings keys and CLI arg groups derive from the class name — they
  diverge if the two don't match.
- Class docstring = module description shown throughout the framework.
- `settings = {"key": {"description": ..., "default": ..., ...}}` defines the
  config schema. Optional keys: `type` (`select`, `text`, `long_text`, `boolean`,
  `number`, `object`, `list`; inferred from `default` if omitted), `options`
  (for `select`), `min`/`max`/`step` (numbers), `depends` (string or dict of
  parent-setting values), `unsafe: true`.
  Full example: `openlumara_docs/dev_docs/core/module.md`.
- `dependencies = ["pip-pkg>=1.x"]` — framework auto pip-installs/uninstalls on
  enable/disable (extracted via AST, module file is never imported to read it).
  Missing deps silently skip loading with a log warning.
- Any public method is an AI tool (JSON schema auto-generated from signature +
  docstring). Methods named `on_*` are event hooks, not tools. `_private` methods
  are hidden. Tools should return `self.result(data, success=True)`.
- Async init happens in `on_ready()`, not `__init__`. `on_background()` runs as a
  persistent asyncio task if its body isn't empty.
- Slash commands: `@core.module.command("name", help=...)` decorator.
- Channels: `async def run()` is the main loop; deliver via `self.send()`,
  `self.send_stream()`, `self.announce()`. Each channel owns its own context/chat.
- Log with `core.log(category, message)` / `self.log(...)`.

## WebUI

- `channels/webui.py`: FastAPI + uvicorn + Jinja2 + WebSockets. Its deps
  (fastapi, uvicorn, bcrypt, ...) are auto-installed since webui is an enabled channel.
- Frontend is Alpine.js + vanilla JS, **no build step, no npm, no bundler**.
  Templates in `channels/webui/templates/`, assets in
  `channels/webui/assets/` — `js/stores/*` are the Alpine stores, `css/` is
  hand-organized. New JS files must be referenced from the template(s) manually.
- `openlumara_docs/dev_docs/channels/webui_frontend.md` describes the OLD
  pre-rewrite frontend — it is stale; trust the code.

## Tests

- stdlib `unittest` only; no pytest config, no CI. Each file in `tests/` is
  self-contained and injects fake `core`/`core.config` modules into `sys.modules`
  so the app is never bootstrapped. Run one: `venv\Scripts\python tests\test_diarization_proxy.py`
  (same pattern per file).
- `tests/test_diarization_host.py` boots `diarization_host/` as a subprocess —
  needs `uvicorn`+`httpx` installed (WebUI deps) and runs the host in mock mode
  (`DIARIZE_MOCK=1`), so no GPU/ffmpeg/models required.

## Audio / STT

- `core/stt.py` powers the WebUI mic/voice/meetings features: settings key
  `stt_engine` (auto|local|server) picks between three engine implementations —
  local is a `whisper-cli` subprocess with binary+model auto-downloaded to
  `data/stt/`, server posts to a whisper.cpp server, and the openai engine uses
  the main API's `/audio/transcriptions` endpoint.
- Server URL resolution: the global channel key
  `channels.settings.webui.stt_whisper_server_url` wins over the per-user
  `api.voice_url` (see `_server_url()` in core/stt.py).

## WIP areas (mostly untracked)

- `diarization_host/`: separate FastAPI service (pyannote, CUDA) that runs on a
  GPU box, not in the main venv (`pip install -r diarization_host/requirements.txt`
  there).
- The in-app proxy `core/diarization.py` referenced by `tests/` is **missing** —
  speaker diarization is mid-implementation. Check git status before assuming
  those tests pass.

## Style & process

- Lint: `.flake8` only (max-line-length 120). No formatter, no pre-commit.
- Async-first (asyncio) everywhere in core/channels/modules.
- PRs go to the `dev` branch upstream (see `CONTRIBUTING.md`); fully
  AI-generated code is not accepted upstream — AI is assistive only.
- Architecture overview: `openlumara_docs/dev_docs/architecture.md` (accurate as
  of the last rewrite of the docs; verify against code when unsure).
