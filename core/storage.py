import core
import os
import json
import yaml
import msgpack

TEMPORARY = False

def _file_ext(file_type: str):
    """file extension for a storage type, or None if the type has no files"""
    match file_type:
        case "text":
            return "txt"
        case "json":
            return "json"
        case "yaml":
            return "yml"
        case "markdown":
            return "md"
        case "msgpack":
            return "mp"
    return None

def storage_file_path(name: str, storage_type: str, base: str) -> str:
    """compute the on-disk path of a storage file without instantiating one.
    base is a resolved data directory (e.g. core.get_data_path())."""
    ext = _file_ext(storage_type)
    path = core.sandbox_path(base, name)
    return f"{path}.{ext}" if ext else path

def atomic_write(path: str, content, binary: bool = False):
    """write content to path atomically via a sibling temp file + os.replace.

    opening the real file with "w" truncates it first, so a crash, kill or
    full disk mid-write leaves a truncated config.yml / chat index / history
    behind. writing a sibling temp file, fsyncing it, and os.replace()ing it
    over the real path is atomic on windows and posix (same directory), so
    readers always see either the old file or the new one, never a partial
    one. raises on failure, leaving the original file untouched.
    """
    tmp_path = f"{path}.tmp"
    write_mode = "wb" if binary else "w"
    encoding = None if binary else "utf-8"
    try:
        with open(tmp_path, write_mode, encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

class StorageList(list):
    """subclassed list that handles storage of data. supports a variety of storage formats."""
    def __init__(self, name: str, type: str, manager=None, path=None, autoload=True, compact_json=False, *args):
        super().__init__(*args)

        # store raw values for lazy resolution
        self.name = name
        self._base_path = path
        self.binary = False
        self.compact_json = compact_json

        # cache for change detection
        self._last_modified = 0.0

        # lets not overwrite a builtin
        file_type = type
        if not type:
            # default to json
            file_type = "json"

        self.binary = file_type == "msgpack"
        file_ext = _file_ext(file_type)

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
            atomic_write(self.path, content, binary=self.binary)
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
        except FileNotFoundError:
            # a missing file is not an error (first run / fresh per-user data)
            return False
        except Exception as e:
            core.log("error", f"error reading {self.name}: {e}")
            return False

    def _file_changed(self):
        """check if the file on disk has changed and cache the new mtime in a single stat"""
        try:
            current_mtime = os.path.getmtime(self.path)
            if current_mtime == self._last_modified:
                return False
            self._last_modified = current_mtime
            return True
        except OSError:
            return True

    def _update_mtime(self):
        """update the cached modification time"""
        try:
            self._last_modified = os.path.getmtime(self.path)
        except OSError:
            pass

    def save(self, snapshot=None):
        """save content to file.

        snapshot: optional list to write instead of the live contents, for
        callers that save from a worker thread while the event loop keeps
        mutating this list (see Messages.save)
        """
        self._resolve_path()
        if TEMPORARY:
            return True

        content = self if snapshot is None else snapshot
        match self.type:
            case "json":
                if self.compact_json:
                    # chat history is rewritten on every message save, so keep its
                    # serialization compact and non-escaped (the pretty + ASCII-escaped
                    # default made it ~2x bigger and slower to dump/write)
                    self._write(json.dumps(content, ensure_ascii=False))
                else:
                    self._write(json.dumps(content, indent=2))
            case "yaml":
                self._write(yaml.safe_dump(content, default_flow_style=False, sort_keys=False, allow_unicode=True))
            case "msgpack":
                self._write(msgpack.packb(content))
            case "text":
                if len(content) > 0:
                    self._write("\n".join(content))

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
            return self

        # read and parse into a temporary before touching memory: clearing
        # first meant a failed or corrupt read left the object empty, and
        # since get() reloads on every access the next save() would wipe the
        # file with empty content
        data = self._read()
        if data is False:
            core.log("error", f"error reading {self.name}, keeping in-memory copy")
            return None

        try:
            match self.type:
                case "json":
                    parsed = json.loads(data)
                case "yaml":
                    parsed = yaml.safe_load(data)
                case "msgpack":
                    parsed = msgpack.unpackb(data)
                case "text":
                    parsed = data.split("\n") if data else []
        except Exception as e:
            core.log("error", f"error parsing {self.name}, keeping in-memory copy: {e}")
            return None

        if parsed is None:
            # an empty yaml document parses to None
            parsed = []

        self.clear()
        self.extend(parsed)

        # update mtime after loading
        self._update_mtime()
        return self

    def get(self, *args, **kwargs):
        if not TEMPORARY:
            self.load()
        if not args:
            return kwargs.get("default", None)
        index = args[0]
        default = args[1] if len(args) > 1 else kwargs.get("default", None)
        if isinstance(index, int) and 0 <= index < len(self):
            return super().__getitem__(index)
        return default

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

        self.binary = file_type == "msgpack"
        file_ext = _file_ext(file_type)

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
            atomic_write(self.path, content, binary=self.binary)
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
        except FileNotFoundError:
            # a missing file is not an error (first run / fresh per-user data)
            return False
        except Exception as e:
            core.log("error", f"error reading {self.name}: {e}")
            return False

    def _file_changed(self):
        """check if the file on disk has changed and cache the new mtime in a single stat"""
        try:
            current_mtime = os.path.getmtime(self.path)
            if current_mtime == self._last_modified:
                return False
            self._last_modified = current_mtime
            return True
        except OSError:
            return True

    def _update_mtime(self):
        """update the cached modification time"""
        try:
            self._last_modified = os.path.getmtime(self.path)
        except OSError:
            pass

    def _parse_nested_keys(self, flat_dict):
        """Convert flat keys like 'ideas/openlumara/topic' into nested dict structure."""
        result = {}
        for key, value in flat_dict.items():
            # normalize separators to / to handle Windows-style paths
            parts = key.replace("\\", "/").split("/")
            current = result
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value
        return result

    def _flatten_nested_keys(self, nested_dict, prefix=""):
        """Convert nested dict into flat keys like 'ideas/openlumara/topic'."""
        result = {}
        for key, value in nested_dict.items():
            full_key = f"{prefix}/{key}" if prefix else key
            if isinstance(value, dict):
                result.update(self._flatten_nested_keys(value, full_key))
            else:
                result[full_key] = value

        return result

    def _delete_nested_key(self, flat_key):
        """Delete a key from the nested dict structure."""
        # normalize the key to ensure consistent splitting
        parts = flat_key.replace("\\", "/").split("/")

        current = self
        # traverse down to the parent dictionary of the target key
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                # the path doesn't exist, nothing to delete
                return

        # delete the target key from the parent dictionary
        if isinstance(current, dict) and parts[-1] in current:
            del current[parts[-1]]

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
            case "markdown":
                # NOTE to readers: i suck at recursive programming, so this is where i heavily use AI assistance. ~Rose22

                # recursive file structure
                # keys like "ideas/openlumara/topic" become nested directories
                if not os.path.exists(self.path):
                    os.makedirs(self.path, exist_ok=True)

                # flatten nested dict to path keys
                flat_items = self._flatten_nested_keys(dict(self))
                failed_keys = []

                for key, content in list(flat_items.items()):
                    try:
                        name = core.sandbox_path(self.path, f"{key}.md")
                    except ValueError as e:
                        # if validation fails, delete the key from the in-memory dicts to keep them clean.
                        self._delete_nested_key(key)
                        del flat_items[key]
                        failed_keys.append((key, str(e)))

                        continue  # Skip saving this file

                    file_dir = os.path.dirname(name)

                    if not os.path.exists(file_dir):
                        os.makedirs(file_dir, exist_ok=True)

                    atomic_write(name, content)

                # Raise an error if any keys were skipped due to validation failure
                if failed_keys:
                    error_msg = "Failed to save the following keys due to validation errors:\n" + "\n".join([f"- {k}: {e}" for k, e in failed_keys])
                    raise ValueError(error_msg)

                # remove files that were deleted
                for root, dirs, files in os.walk(self.path, topdown=False):
                    for filename in files:
                        if filename.endswith(".md"):
                            full_path = os.path.join(root, filename)
                            rel_path = os.path.relpath(full_path, self.path)

                            # remove the .md extension
                            path_no_ext = rel_path[:-3]

                            # normalize path to make it cross-platform
                            normalized = os.path.normpath(path_no_ext)
                            logical_key = "/".join(normalized.split(os.sep))

                            if logical_key not in flat_items:
                                os.remove(full_path)

                    # remove empty directories
                    if root != self.path and not os.listdir(root):
                        os.rmdir(root)
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
        if self.type not in ["markdown"] and not self._file_changed():
            return True

        # read and parse into a temporary before touching memory: clearing
        # first meant a failed or corrupt read left the object empty, and
        # since get() reloads on every access the next save() would wipe the
        # file with empty content
        if self.type not in ["markdown"]:
            data = self._read()
            if data is False:
                core.log("error", f"error reading {self.name}, keeping in-memory copy")
                return None

        try:
            match self.type:
                case "json":
                    parsed = json.loads(data)
                case "yaml":
                    parsed = yaml.safe_load(data)
                case "markdown":
                    # recursive file structure
                    flat_dict = {}
                    for root, dirs, files in os.walk(self.path):
                        for filename in files:
                            if filename.endswith(".md"):
                                full_path = os.path.join(root, filename)
                                rel_path = os.path.relpath(os.path.join(root, filename), self.path)

                                # remove .md extension
                                path_without_ext = rel_path[:-3]

                                # normalize path to make it cross-platform
                                normalized_path = os.path.normpath(path_without_ext)
                                key = "/".join(normalized_path.split(os.sep))

                                with open(full_path, "r", encoding="utf-8") as f:
                                    flat_dict[key] = str(f.read())

                    # convert flat path keys to nested dict structure
                    parsed = self._parse_nested_keys(flat_dict)
                case "msgpack":
                    parsed = msgpack.unpackb(data)
                case "text":
                    parsed = data.split("\n")
        except Exception as e:
            core.log("error", f"error parsing {self.name}, keeping in-memory copy: {e}")
            return None

        if parsed is None:
            # an empty yaml document parses to None
            parsed = {}

        self.clear()
        self.update(parsed)

        # update mtime after loading
        self._update_mtime()
        return True

    def get(self, *args, **kwargs):
        if not TEMPORARY and not self.override_temporary:
            self.load()

        return super().get(*args, **kwargs)
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

        atomic_write(self.path, self._data)

        # update mtime after saving so we know our cache is fresh
        self._update_mtime()
        return self

    def _file_changed(self):
        """check if the file on disk has changed and cache the new mtime in a single stat"""
        try:
            current_mtime = os.path.getmtime(self.path)
            if current_mtime == self._last_modified:
                return False
            self._last_modified = current_mtime
            return True
        except OSError:
            return True

    def _update_mtime(self):
        """update the cached modification time"""
        try:
            self._last_modified = os.path.getmtime(self.path)
        except OSError:
            pass
