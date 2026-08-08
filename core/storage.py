import core
import os
import json
import yaml
import msgpack

TEMPORARY = False

class StorageList(list):
    """subclassed list that handles storage of data. supports a variety of storage formats."""
    def __init__(self, name: str, type: str, manager=None, path=None, autoload=True, *args):
        super().__init__(*args)

        # store raw values for lazy resolution
        self.name = name
        self._base_path = path
        self.binary = False

        # cache for change detection
        self._last_modified = 0.0

        # lets not overwrite a builtin
        file_type = type
        if not type:
            # default to json
            file_type = "json"

        file_ext = None
        match file_type:
            case "text":
                file_ext = "txt"
            case "json":
                file_ext = "json"
            case "yaml":
                file_ext = "yml"
            case "msgpack":
                file_ext = "mp"
                self.binary = True

        self.type = file_type
        self.ext = file_ext
        self._path_resolved = False

        if manager:
            self.manager = manager

        # resolve path lazily for load/save
        self._resolve_path()

        if os.path.exists(self.path):
            if autoload and not TEMPORARY:
                self.load()
        else:
            self.save()

    def _resolve_path(self):
        """Resolve the final path using get_data_path() for user-scoped data."""
        if self._path_resolved:
            return

        base = self._base_path if self._base_path else core.get_data_path()
        self.path = core.sandbox_path(base, self.name)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.path += f".{self.ext}"
        self._path_resolved = True

    def _write(self, content):
        try:
            write_mode = "wb" if self.binary else "w"
            encoding = "utf-8" if not self.binary else None

            with open(self.path, write_mode, encoding=encoding) as f:
                f.write(content)
        except Exception as e:
            core.log("error", f"error writing {self.name}: {e}")
            return False

        return True
    def _read(self):
        try:
            result = None
            read_mode = "rb" if self.binary else "r"
            encoding = "utf-8" if not self.binary else None
            with open(self.path, read_mode, encoding=encoding) as f:
                result = f.read()
            return result
        except Exception as e:
            core.log("error", f"error reading {self.name}: {e}")
            return False

    def _file_changed(self):
        """check if the file on disk has changed"""
        try:
            current_mtime = os.path.getmtime(self.path)
            return current_mtime != self._last_modified
        except OSError:
            return True

    def _update_mtime(self):
        """update the cached modification time"""
        try:
            self._last_modified = os.path.getmtime(self.path)
        except OSError:
            pass

    def save(self):
        """save content to file"""
        self._resolve_path()
        if TEMPORARY:
            return True

        match self.type:
            case "json":
                self._write(json.dumps(self, indent=2))
            case "yaml":
                self._write(yaml.safe_dump(self, default_flow_style=False, sort_keys=False, allow_unicode=True))
            case "msgpack":
                self._write(msgpack.packb(self))
            case "text":
                if len(self) > 0:
                    self._write("\n".join(self))

        # update mtime after saving so we know our cache is fresh
        self._update_mtime()

    def load(self, data=None):
        """load content from file or data argument"""
        self._resolve_path()
        if data is not None:
            self.clear()
            self.extend(data)
            return self

        # skip reload if file hasn't changed on disk
        if not self._file_changed():
            self._update_mtime()
            return self

        self.clear()

        data = self._read()
        if not data:
            return None

        match self.type:
            case "json":
                self.extend(json.loads(data))
            case "yaml":
                self.extend(yaml.safe_load(data))
            case "msgpack":
                self.extend(msgpack.unpackb(data))
            case "text":
                self.extend(data.split("\n"))

        # update mtime after loading
        self._update_mtime()

    def get(self, *args, **kwargs):
        if not TEMPORARY:
            self.load()

        return super().__getitem__(args[0])

class StorageDict(dict):
    """subclassed dict that handles storage of data. supports a variety of storage formats."""
    def __init__(self, name: str, type: str, manager=None, path=None, autoload=True, override_temporary=False, *args):
        super().__init__(*args)

        # store raw values for lazy resolution
        self.name = name
        self._base_path = path
        self.binary = False

        # this is mainly for the config, so that we can still make changes in temporary mode
        # but who knows what it might be needed for in the future
        self.override_temporary = override_temporary

        # cache for change detection
        self._last_modified = 0.0

        # lets not overwrite a builtin
        file_type = type
        if not type:
            # default to json
            file_type = "json"

        file_ext = None
        match file_type:
            case "text":
                file_ext = "txt"
            case "json":
                file_ext = "json"
            case "yaml":
                file_ext = "yml"
            case "msgpack":
                file_ext = "mp"
                self.binary = True

        self.type = file_type
        self.ext = file_ext
        self._path_resolved = False

        if manager:
            self.manager = manager

        # resolve path lazily for load/save
        self._resolve_path()

        if os.path.exists(self.path):
            if autoload and not (TEMPORARY and not self.override_temporary):
                self.load()
        else:
            self.save()

    def _resolve_path(self):
        """Resolve the final path using get_data_path() for user-scoped data."""
        if self._path_resolved:
            return

        base = self._base_path if self._base_path else core.get_data_path()
        self.path = core.sandbox_path(base, self.name)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.path += f".{self.ext}"
        self._path_resolved = True

    def _write(self, content):
        try:
            write_mode = "wb" if self.binary else "w"
            encoding = "utf-8" if not self.binary else None
            with open(self.path, write_mode, encoding=encoding) as f:
                f.write(content)
        except Exception as e:
            core.log("error", f"error writing {self.name}: {e}")
            return False

        return True

    def _read(self):
        try:
            result = None
            read_mode = "rb" if self.binary else "r"
            encoding = "utf-8" if not self.binary else None
            with open(self.path, read_mode, encoding=encoding) as f:
                result = f.read()
            return result
        except Exception as e:
            core.log("error", f"error reading {self.name}: {e}")
            return False

    def _file_changed(self):
        """check if the file on disk has changed"""
        try:
            current_mtime = os.path.getmtime(self.path)
            return current_mtime != self._last_modified
        except OSError:
            return True

    def _update_mtime(self):
        """update the cached modification time"""
        try:
            self._last_modified = os.path.getmtime(self.path)
        except OSError:
            pass

    def save(self):
        """save content to file"""
        self._resolve_path()
        if TEMPORARY and not self.override_temporary:
            return True

        match self.type:
            case "json":
                self._write(json.dumps(dict(self), indent=2))
            case "yaml":
                self._write(yaml.safe_dump(dict(self), default_flow_style=False, sort_keys=False, allow_unicode=True))
            case "msgpack":
                self._write(msgpack.packb(dict(self)))
            case "text":
                if len(self) > 0:
                    self._write("\n".join(dict(self)))

        # update mtime after saving so we know our cache is fresh
        self._update_mtime()

    def load(self, data=None):
        """load content from file or data argument"""
        self._resolve_path()
        if data is not None:
            self.clear()
            self.update(data)
            return True

        # skip reload if file hasn't changed on disk
        if not self._file_changed():
            self._update_mtime()
            return True

        self.clear()

        data = self._read()
        if not data:
            return None

        match self.type:
            case "json":
                self.update(json.loads(data))
            case "yaml":
                self.update(yaml.safe_load(data))
            case "msgpack":
                self.update(msgpack.unpackb(data))
            case "text":
                self.update(data.split("\n"))

        # update mtime after loading
        self._update_mtime()
        return True

    def get(self, *args, **kwargs):
        if not TEMPORARY and not self.override_temporary:
            self.load()

        return super().get(*args)

class StorageText:
    """simple class that saves its content to a text file"""
    def __init__(self, name: str, manager=None, path=None, autoload=True, *args):
        super().__init__(*args)

        # store raw values for lazy resolution
        self.name = name
        self._base_path = path
        self._data = ""

        # cache for change detection
        self._last_modified = 0.0
        self._path_resolved = False

        if manager:
            self.manager = manager

        # resolve path lazily for load/save
        self._resolve_path()

        if os.path.exists(self.path):
            if autoload and not TEMPORARY:
                self.load()
        else:
            self.save()

    def _resolve_path(self):
        """Resolve the final path using get_data_path() for user-scoped data."""
        if self._path_resolved:
            return

        base = self._base_path if self._base_path else core.get_data_path()
        self.path = core.sandbox_path(base, self.name)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._path_resolved = True

    def __str__(self, *args, **kwargs):
        return self.get()

    def set(self, new_data: str):
        self._data = str(new_data)
        self.save()
    def get(self):
        self._resolve_path()
        if not TEMPORARY:
            self.load()
        return str(self._data)

    def load(self):
        self._resolve_path()
        # skip reload if file hasn't changed on disk
        if not self._file_changed():
            self._update_mtime()
            return self

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = f.read()
        except Exception as e:
            core.log("error", f"error while loading text storage: {e}")

        # update mtime after loading
        self._update_mtime()
        return self

    def save(self):
        self._resolve_path()
        if TEMPORARY:
            return self

        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self._data)

        # update mtime after saving so we know our cache is fresh
        self._update_mtime()
        return self

    def _file_changed(self):
        """check if the file on disk has changed"""
        try:
            current_mtime = os.path.getmtime(self.path)
            return current_mtime != self._last_modified
        except OSError:
            return True

    def _update_mtime(self):
        """update the cached modification time"""
        try:
            self._last_modified = os.path.getmtime(self.path)
        except OSError:
            pass
