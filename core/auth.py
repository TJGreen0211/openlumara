import os
import re
import json
import bcrypt
import datetime
import shutil

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,32}$')

class UserManager:
    def __init__(self, data_folder):
        self.data_folder = data_folder
        self.users_file = os.path.join(data_folder, "users.json")
        self._cache = None
        self._cache_mtime = 0.0
        self._revoked_sessions = set()

    def _file_mtime(self):
        try:
            return os.path.getmtime(self.users_file)
        except OSError:
            return 0

    def _load(self):
        mtime = self._file_mtime()
        if self._cache is not None and self._cache_mtime == mtime:
            return self._cache
        if not os.path.exists(self.users_file):
            self._cache = {"users": {}}
            self._cache_mtime = mtime
            return self._cache
        try:
            with open(self.users_file, "r", encoding="utf-8") as f:
                if _HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
                if _HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            # Validate data structure
            if not isinstance(data, dict) or "users" not in data or not isinstance(data["users"], dict):
                data = {"users": {}}
            self._cache = data
            self._cache_mtime = mtime
            return self._cache
        except (json.JSONDecodeError, OSError):
            self._cache = {"users": {}}
            self._cache_mtime = mtime
            return self._cache

    def _save(self, data):
        try:
            os.makedirs(os.path.dirname(self.users_file), exist_ok=True)
            with open(self.users_file, "w", encoding="utf-8") as f:
                if _HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
                if _HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            self._cache = data
            self._cache_mtime = self._file_mtime()
            return True
        except OSError:
            return False

    def _validate_username(self, username):
        if not username or not _USERNAME_RE.match(username):
            return False
        if os.path.sep in username or username.startswith('.') or username in ('..', '.', 'users.json'):
            return False
        return True

    def invalidate_session(self, session_id):
        self._revoked_sessions.add(session_id)

    def is_session_revoked(self, session_id):
        return session_id in self._revoked_sessions

    def create_user(self, username, password, role="user"):
        if not self._validate_username(username):
            return None
        data = self._load()
        if username in data["users"]:
            return None

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
        now = datetime.datetime.now().isoformat()

        data["users"][username] = {
            "password_hash": password_hash,
            "role": role,
            "created_at": now,
            "last_login": None
        }

        self._save(data)
        self._ensure_user_dir(username)
        return data["users"][username]

    def authenticate(self, username, password):
        data = self._load()
        user = data["users"].get(username)
        if not user:
            return None

        password_hash = user.get("password_hash")
        if not password_hash:
            return None

        if not bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")):
            return None

        now = datetime.datetime.now().isoformat()
        data["users"][username]["last_login"] = now
        self._save(data)

        return {
            "username": username,
            "role": user.get("role"),
            "created_at": user.get("created_at"),
            "last_login": now
        }

    def delete_user(self, username):
        data = self._load()
        if username not in data["users"]:
            return False

        if self._count_admins(data) <= 1 and data["users"][username].get("role") == "admin":
            return False

        del data["users"][username]
        self._save(data)
        self._invalidate_user_sessions(username)

        user_dir = os.path.join(self.data_folder, username)
        if os.path.exists(user_dir):
            shutil.rmtree(user_dir)

        return True

    def _invalidate_user_sessions(self, username):
        pass

    def change_password(self, username, new_password):
        data = self._load()
        if username not in data["users"]:
            return False

        new_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
        data["users"][username]["password_hash"] = new_hash
        self._save(data)
        return True

    def update_role(self, username, role):
        data = self._load()
        if username not in data["users"]:
            return False

        current_role = data["users"][username].get("role")
        if current_role == "admin" and role != "admin" and self._count_admins(data) <= 1:
            return False

        data["users"][username]["role"] = role
        self._save(data)
        return True

    def list_users(self):
        data = self._load()
        users = []
        for username, udata in data["users"].items():
            users.append({
                "username": username,
                "role": udata.get("role"),
                "created_at": udata.get("created_at"),
                "last_login": udata.get("last_login")
            })
        return users

    def get_user(self, username):
        data = self._load()
        user = data["users"].get(username)
        if not user:
            return None
        return {
            "username": username,
            "role": user.get("role"),
            "created_at": user.get("created_at"),
            "last_login": user.get("last_login")
        }

    def _count_admins(self, data=None):
        if data is None:
            data = self._load()
        count = 0
        for udata in data["users"].values():
            if udata.get("role") == "admin":
                count += 1
        return count

    def _ensure_user_dir(self, username):
        user_dir = os.path.join(self.data_folder, username)
        os.makedirs(user_dir, exist_ok=True)
