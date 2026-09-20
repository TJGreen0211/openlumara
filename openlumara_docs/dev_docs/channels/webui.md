# WebUI Channel (Backend)

The `Webui` channel is implemented using **FastAPI**, providing a high-performance, asynchronous web interface for interacting with OpenLumara. It serves both as a web server for the frontend and as an API for client-side interactions.

## Key Features

- **Asynchronous Support**: Native `asyncio` support via FastAPI.
- **Real-time Communication**: Uses **WebSockets** (`/ws`) to broadcast message updates, status changes, and metadata updates (like chat titles) to all connected clients.
- **Authentication**: Supports both session-based authentication (for browser users) and Bearer token authentication (for API/client access).
- **Streaming API**: Implements an event-stream (`/stream`) endpoint that allows clients to receive token-by-token AI responses in real-time.
- **Storage Editor**: Provides built-in API endpoints to browse, load, and edit configuration and data files (JSON, YAML, MsgPack, Text, MD) directly through the WebUI.

## API Endpoints

### Chat Management
- `GET /chats`: Lists all available chats with previews.
- `GET /chat/load?id=<id>`: Loads a specific chat.
- `GET /chat/current`: Gets the currently active chat.
- `POST /chat/new`: Creates a new chat.
- `POST /chat/rename`: Renames the current chat.
- `POST /chat/update_category`: Updates the category of a chat.
- `POST /chat/tag`: Adds a tag to the current chat.
- `POST /chat/delete`: Deletes a chat.
- `POST /chat/clear`: Clears the current chat.

### Message Operations
- `GET /messages`: Retrieves all messages in the current chat.
- `GET /messages/since?index=<index>`: Retrieves messages starting from a specific index (efficient for incremental updates).
- `POST /send`: Sends a single message (non-streaming).
- `POST /stream`: Starts an asynchronous stream of AI tokens.
- `POST /edit`: Edits a message by its index.
- `POST /delete`: Deletes a message by its index.
- `POST /upload`: Handles file uploads (images or text files).

### Settings & Configuration
- `GET /settings/load`: Retrieves the current system configuration.
- `POST /settings/save`: Saves new configuration settings.
- `GET /settings/get_module_info`: Retrieves metadata and settings schemas for all loaded modules.

### Storage Editor
- `GET /storage/list`: Lists all files in the data directory.
- `GET /storage/load?file=<path>`: Loads a specific storage file.
- `POST /storage/save`: Saves changes to a storage file.
- `POST /storage/delete-key`: Deletes a key from a dictionary-based file.
- `POST /storage/add-key`: Adds a new key to a dictionary-based file.

### System & API
- `GET /api/status`: Returns the current API connection status, including model and configuration info.
- `POST /api/reconnect`: Attempts to reconnect the channel to the AI API.
- `POST /api/disconnect`: Disconnects the channel from the AI API.
- `GET /api/models`: Lists available models from the configured API.
- `POST /server/restart`: Triggers a server restart.

## WebSocket Events

The WebSocket connection (`/ws`) handles real-time events:
- `message_added`: Broadcasts a new message to all clients.
- `chat_metadata_updated`: Notifies clients of title or tag changes.
- `status_updated`: Communicates connection/status changes.
- `stop`: Signals the API to stop the current request.
- `cancel`: Cancels a specific stream by ID.

## Multi-user Mode

Setting `webui -> require_login` to `true` turns the WebUI into a multi-account server. The core isolation primitives:

- **`core.current_user`** (`core/__init__.py`): a `contextvars.ContextVar` holding the active username. It's set by the HTTP middleware (per request), by the WebSocket message loop (per message), and by background loops (e.g. the scheduler dispatcher) via set/reset tokens. Storage paths, config merging, and `channel.context` all key off it.
- **Per-user data**: with the contextvar set, `core.get_data_path()` resolves to `data/{username}/`. Storage created under a user context (chat index, per-owner calendar/schedule lists) is isolated per user. Module data (notes, memory, lists, identity) is per-user too: modules call `Module.user_storage()` (`core/module.py`), which lazily creates and caches one storage instance per user, pinned to `data/{username}/`. On a user's first access their file is seeded from the legacy global file (`data/{name}.{ext}`) if it exists, so single-user setups keep their data after a multi-user split. Instances without a user context (all non-webui channels) keep using the shared `data/` root.
- **Per-user contexts**: the channel caches a `core.context.Context` per user in `channel.user_contexts`, built and loaded while the user's contextvar is set (see `Webui.context` property and `_get_user_context()`). Each `Context` (and its `Chat`) pins its storage paths at construction, so the contextvar must be correct when the Context is created.
- **Per-user config**: `core.config.get()` merges the user's `data/{username}/config.json` over the global config for the per-user sections (`api`, `model`, `appearance`, `audio` + selected `core` keys + module `settings`). Writes route through `core.config.set_user_or_global()`. The shared AI API connection is instance-wide - only values like model name and per-module settings are truly per-user.
- **Per-user streams**: `WebSocketManager.streams` holds one background stream task + cancel `asyncio.Event` per connected user, so concurrent users don't cancel each other's streams. `stop` sets only the requester's token. `chat_switched` broadcasts and pushes are scoped by username; log broadcasts are admin-only.
- **Permissions**: `_require_admin()` (re-validates the role against the user store) guards user management; `_system_authorize()` guards restart/reconnect/logs (single-user mode bypasses both, since there are no roles). Non-admin `settings/save` drops global sections on the backend; the frontend additionally hides global categories/toggles for non-admins (gated by `data-user-role` on the `<html>` element). Chat admin commands (`/restart`, `/coder`, ... ) are gated by `Webui._commands_authorized_for()` - in multi-user mode only admins may run them. `/stop` resolves the current user's `Context.cancel_token`, so it stops only their own stream, never other users' in-flight streams.
- **Scheduler & calendar**: jobs/events record their owner's username in a `user` field and live in per-owner storage lists; the dispatcher/notification loops enumerate all owners and restore the owner's contextvar (set/reset token) before executing, so pushes land in the right user's chat.
