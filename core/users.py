import core
import bcrypt
import ulid
import datetime
import os
import shutil
import json
from dataclasses import dataclass, asdict

@dataclass
class User:
    id: str
    username: str
    password_hash: str
    role: str  # "admin" or "user"
    created: str  # ISO timestamp

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            id=data["id"],
            username=data["username"],
            password_hash=data["password_hash"],
            role=data["role"],
            created=data["created"]
        )

class UserStore:
    """Manages user persistence via StorageList → data/users.json"""

    def __init__(self):
        self._store = core.storage.StorageList("users", "json")

    def _save(self):
        self._store.save()

    def _reload(self):
        self._store.load()

    def _user_exists(self, username: str) -> bool:
        self._reload()
        for item in self._store:
            if isinstance(item, dict) and item.get("username") == username:
                return True
        return False

    def create_user(self, username: str, password: str, role: str = "user") -> User:
        if self._user_exists(username):
            raise ValueError(f"User '{username}' already exists")

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        user = User(
            id=str(ulid.ULID())[:8],
            username=username,
            password_hash=password_hash,
            role=role,
            created=datetime.datetime.utcnow().isoformat()
        )

        self._store.append(user.to_dict())
        self._save()
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        self._reload()
        for item in self._store:
            if isinstance(item, dict) and item.get("username") == username:
                user = User.from_dict(item)
                if bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8")):
                    return user
        return None

    def get_user(self, username: str) -> User | None:
        self._reload()
        for item in self._store:
            if isinstance(item, dict) and item.get("username") == username:
                return User.from_dict(item)
        return None

    def get_users(self) -> list[User]:
        self._reload()
        return [User.from_dict(item) for item in self._store if isinstance(item, dict)]

    def update_user(self, username: str, **fields) -> User | None:
        self._reload()
        for i, item in enumerate(self._store):
            if isinstance(item, dict) and item.get("username") == username:
                if "password" in fields:
                    fields["password_hash"] = bcrypt.hashpw(fields.pop("password").encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

                for key, value in fields.items():
                    if key in ("password_hash", "role"):
                        self._store[i][key] = value

                self._save()
                return User.from_dict(self._store[i])
        return None

    def delete_user(self, username: str) -> bool:
        self._reload()
        for i, item in enumerate(self._store):
            if isinstance(item, dict) and item.get("username") == username:
                self._store.pop(i)
                self._save()
                user_data_path = core.functions.get_user_data_path(username)
                if os.path.exists(user_data_path):
                    try:
                        shutil.rmtree(user_data_path)
                    except Exception as e:
                        core.log("error", f"Failed to remove user data folder for '{username}': {e}")
                return True
        return False

    def is_admin(self, username: str) -> bool:
        user = self.get_user(username)
        return user is not None and user.role == "admin"

    def migrate_from_config(self):
        """Reads old config credentials, creates admin user, copies existing chat files."""
        webui_config = core.config.get("channels", {}).get("settings", {}).get("webui", {})
        old_username = webui_config.get("username")
        old_password = webui_config.get("password")

        if not old_username or not old_password:
            core.log("error", "Cannot migrate: no existing credentials found in config")
            return False

        # Create admin user from old credentials
        user_created = False
        try:
            self.create_user(old_username, old_password, role="admin")
            user_created = True
        except ValueError:
            # User already exists, ensure they have admin role
            existing = self.get_user(old_username)
            if existing and existing.role != "admin":
                self.update_user(old_username, role="admin")

        # Copy existing chat files into the user's data folder
        base_data_path = core.get_data_path()
        user_data_path = core.functions.get_user_data_path(old_username)
        os.makedirs(user_data_path, exist_ok=True)

        migrated = False
        for filename in os.listdir(base_data_path):
            filepath = os.path.join(base_data_path, filename)
            # Only migrate chat-related files (json files with _chats suffix, and current_chat files)
            if "_chats.json" in filename or "_current_chat" in filename or "_current_chat.json" in filename:
                dest = os.path.join(user_data_path, filename)
                if os.path.isfile(filepath) and not os.path.exists(dest):
                    shutil.copy2(filepath, dest)
                    migrated = True
                    core.log("info", f"Migrated {filename} → users/{old_username}/")

        if user_created:
            core.log("info", f"Migration complete: created admin user '{old_username}'")
        elif migrated:
            core.log("info", f"Migration complete: copied chat files for existing user '{old_username}'")

        return user_created or migrated
