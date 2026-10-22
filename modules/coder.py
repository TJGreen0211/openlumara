import core
import os
import asyncio
import html
import ipaddress
import urllib.parse
import importlib
import shutil
import regex
import glob
import fnmatch
import filetype
try:
    import tree_sitter_language_pack as tslp
except ImportError:
    tslp = None

class Coder(core.module.Module):
    """Lets your AI write code within sandboxes, without using a shell. Validates code syntax before writing to disk to protect your code from breaking."""

    # The coder module, now manually rewritten and free of any AI slop :)
    # ~ Rose22

    settings = {
        "read-only": {
            "default": False,
            "description": "Disables all operations that could modify your code or your filesystem. You can use `/coder readonly` to quickly toggle this during sessions."
        },
        "global_line_limit": {
            "default": 1000,
            "description": "Hard limit on the amount of lines the AI is allowed to read at once. Greatly helps reduce token usage!"
        },

        # paths
        "sandbox_paths": {
            "default": ["coder"],
            "description": "Folders you want the coder to have access to. A plain name (like `coder`) is a sandbox inside the data folder (`data/coder/<name>`, or `data/<username>/coder/<name>` per user). You can also give a full path to any folder on disk, and use the `~` character to refer to your home folder, such as `/home/you` on linux, /Users/You on mac, `C:\\Users\\You` on windows"
        },
        "per_user_sandboxes": {
            "default": True,
            "description": "In multi-user WebUI mode, gives every user their own private sandboxes (in their own data folder, or `<sandbox>/users/<username>` for sandboxes given as a full path), so users can't see or change each other's files and sites. Has no effect in single-user mode."
        },
        "max_media_size_mb": {
            "default": 15,
            "min": 1,
            "max": 100,
            "description": "Largest image or video (in MB) that can be added to a site's media folder, through uploads or URL imports."
        },

        "template_paths": {
            "default": [],
            "description": "Templates are special files that the coder can read from anywhere at any time when requested, in order to show example code to the AI. You can define folders to load such templates from here!"
        },
        "enable_builtin_templates": {
            "default": True,
            "description": "Openlumara comes bundled with hand-written templates for creating your own modules and channels! Enable them here.\n\n**TIP**: To vibecode your own modules or channels, tell the AI to read the module or channel template in your prompt. Example: `read the openlumara channel template and make me a basic terminal user interface`"
        },

        "use_coding_prompt": {
            "default": False,
            "description": "If enabled, will allow you to specify a special prompt that instructs the AI on your preferred coding practices and guidelines."
        },
        "coding_prompt": {
            "type": "long_text",
            "default": "",
            "depends": "use_coding_prompt"
        },

        # flags
        "insert_sandbox_paths_into_system_prompt": {
            "default": True,
            "description": "Puts the list of sandboxes into the AI's system prompt so that it is always aware of which paths it can access. Recommended!"
        },
        "add_sandbox_contents_to_sandbox_list": {
            "default": False,
            "description": "Puts a list of top-level folders within each sandbox into the system prompt. This greatly speeds up sandbox exploration, and is not a recursive list - it only lists the folders that are directly at the root level of each sandbox.",
            "depends": "insert_sandbox_paths_into_system_prompt"
        },

        "show_preview_hint": {
            "default": True,
            "description": "Instructs the AI to call open_preview with the entry HTML file after building a website."
        },

        # blacklists
        "folder_blacklist": {
            "default": ["venv", "__pycache__"],
            "description": "Prevents recursive operations such as `glob` and `grep` from reaching into folders matching the names in this blacklist"
        }
    }

    # the coder likes sitting on a tree... of code
    # the tree is nice and green and it also tells the coder whether it made any oopsies
    dependencies = [
        "tree-sitter",
        "tree-sitter-language-pack",
    ]

    regex_timeout = 3
    builtin_templates_path = "modules/coder/templates"

    # sandboxes given as a relative path live in this folder inside the (per-user) data folder,
    # eg. data/<username>/coder/<sandbox>, so they're deleted along with the user's account
    data_folder = "coder"
    # the old default sandbox location. it's moved into the data folder on first use
    legacy_sandbox_path = "~/coder"

    # every site keeps the user's images/videos in this folder, next to its entry html file
    media_folder = "media"
    # sniffed mime type -> file extension. anything else is refused, so a "photo"
    # can't smuggle an executable or html page into a site
    media_types = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
        "image/x-icon": "ico",
        "image/svg+xml": "svg",
        "video/mp4": "mp4",
        "video/webm": "webm",
    }
    # files that get their media references rewritten when a placeholder is replaced
    site_text_extensions = (".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg")

    # -------------
    # events
    # -------------
    async def on_ready(self):
        self.disabled_tools = []

        self._migrate_legacy_config()

        # enable/disable tools based on selected modes
        if self.config.get("read-only"):
            self.disabled_tools.extend(["file_create", "file_move", "file_delete", "file_edit", "folder_delete",
                                        "media_placeholder", "media_import_url"])

        if self.config.get("insert_sandbox_paths_into_system_prompt"):
            self.disabled_tools.append("list_sandboxes")

        # still figuring out how to best guide the AI in when to use file_inspect over file_read.
        # it's a very tough thing since most LLM's are trained to just read entire files
        # and are inclined to do so
        self.disabled_tools.append("file_outline")

    async def on_system_prompt(self):
        final_output = []

        templates = await self._get_templates()
        if templates:
            output_str = "## Templates you can read using read_template:\n"
            output_str += "\n".join([f"- {template}" for template in templates])
            final_output.append(output_str)

        if self.config.get("insert_sandbox_paths_into_system_prompt"):
            sandboxes = await self._get_sandbox_paths()
            if sandboxes:
                output_str = "## Sandboxes you have access to:\n"
                output_str += "\n".join([f"- {path}" for path in sandboxes])
                final_output.append(output_str)

            if self.config.get("add_sandbox_contents_to_sandbox_list"):
                for sandbox in sandboxes:
                    output_str = f"### Files within sandbox '{sandbox}':"
                    full_path = await self._get_full_sandbox_path(sandbox)
                    folder_list = os.listdir(full_path)

                    files = []
                    folders = []
                    for file in folder_list:
                        if os.path.isdir(os.path.join(full_path, file)):
                            folders.append(file)
                        else:
                            files.append(file)

                    folders.sort()
                    files.sort()

                    if folders:
                        output_str += "\n#### Folders:\n"
                        output_str += "\n".join([f"- {folder}" for folder in folders])
                    if files:
                        output_str += "\n#### Files:\n"
                        output_str += "\n".join([f"- {file}" for file in files])

                    final_output.append(output_str)

        if self.config.get("use_coding_prompt") and self.config.get("coding_prompt"):
            final_output.append(f"## Coding Guidelines\nYOU MUST ALWAYS follow these guidelines while using the coder:\n{self.config.get('coding_prompt')}")

        if self.config.get("show_preview_hint") and not self.config.get("read-only"):
            final_output.append(
                "## Live Preview\nAfter building an HTML site the user should see, call open_preview with the entry "
                "HTML file (e.g. index.html) so it renders in the WebUI's preview panel. The user will then ask for "
                "changes in plain language - apply them with the file tools, then call open_preview again.\n"
                "Give every separate site its own folder (e.g. `bakery/index.html`, `portfolio/index.html`); pages of "
                "the same site live in that folder.\n\n"
                "## Site Media\n"
                f"- A site keeps its images and videos in a `{self.media_folder}/` folder next to its entry HTML "
                f"file. Reference them with relative paths (`{self.media_folder}/hero.jpg`, or "
                f"`../{self.media_folder}/hero.jpg` from a page in a sub-folder). Never start the path with `/` - "
                "that points at the WebUI, not the site.\n"
                "- Files the user attaches are saved into that folder automatically; their message tells you the "
                "paths.\n"
                "- When the site needs an image the user hasn't provided, call media_placeholder and use the path it "
                "returns. Style images with `object-fit: cover` so a real photo of another shape fits later. Never "
                "hotlink stock photos or external placeholder services.\n"
                "- When the user gives you an image or video URL, call media_import_url and use the path it returns."
            )

        if self.config.get("read-only"):
            final_output.append("## IMPORTANT: Read-Only Mode\nYour coder module is in read-only mode and you cannot write to files. Provide output code to the user directly in your messages.")

        if final_output:
            return "\n\n".join(final_output)
        return None

    # -------------------------------
    # helper functions: sandbox stuff
    # -------------------------------
    async def _get_sandbox_paths(self):
        """
        translates the sandbox path list into basenames
        so that it can actually be used by the AI without
        having to recite full paths all the time
        """
        paths = self.config.get("sandbox_paths")
        if not paths:
            return []

        # create them if they dont exist
        for path in paths:
            self._resolve_sandbox_root(path)

        paths = [path.rstrip("/\\") for path in paths]
        return [os.path.basename(path) for path in paths]

    async def _get_full_sandbox_path(self, requested_path: str):
        """converts a basename'd sandbox path back into the full path"""
        paths = self.config.get("sandbox_paths")
        if not paths:
            return None

        for path in paths:
            if os.path.basename(path.rstrip("/\\")) == requested_path.rstrip("/\\"):
                return self._resolve_sandbox_root(path)

        raise Exception("That sandbox does not exist")

    def _resolve_sandbox_root(self, path: str):
        """
        turns a configured sandbox path into the folder the current user works in.
        full paths are used as-is (scoped per user), plain names live in the
        data folder: data/coder/<name>, or data/<username>/coder/<name> per user
        """
        path = self._replace_legacy_path(path)
        path = os.path.expanduser(path).rstrip("/\\")
        if os.path.isabs(path):
            return self._user_scoped_path(path)

        user = core.current_user.get() if self.config.get("per_user_sandboxes") else None
        # user="" forces the global data folder even while a user is active
        root = core.get_data_path(os.path.join(self.data_folder, path), user=user or "")
        self._migrate_legacy_sandbox(path, user, root)
        os.makedirs(root, exist_ok=True)
        return root

    def _replace_legacy_path(self, path: str):
        """the old ~/coder default is the data folder sandbox of the same name now"""
        if path.rstrip("/\\") == self.legacy_sandbox_path:
            return os.path.basename(self.legacy_sandbox_path)
        return path

    def _migrate_legacy_config(self):
        """
        swaps the old ~/coder default for the data folder sandbox in the global config
        and in every user's config.json (which can override module settings).
        paths are also swapped as they're resolved, this keeps the settings ui honest
        """
        paths = self.config.get("sandbox_paths") or []
        if any(self._replace_legacy_path(path) != path for path in paths):
            self.config.set("sandbox_paths", [self._replace_legacy_path(path) for path in paths])

        data_root = core.get_data_path(user="")
        for username in os.listdir(data_root):
            if not os.path.isfile(os.path.join(data_root, username, "config.json")):
                continue
            user_config = core.config.load_user_config(username)
            settings = user_config.get("modules", {}).get("settings", {}).get(self.name, {})
            paths = settings.get("sandbox_paths")
            if isinstance(paths, list) and any(self._replace_legacy_path(path) != path for path in paths):
                settings["sandbox_paths"] = [self._replace_legacy_path(path) for path in paths]
                core.config.save_user_config(username, user_config)

    def _migrate_legacy_sandbox(self, name: str, user: str, root: str):
        """
        moves files left in the old ~/coder sandbox into its data folder location.
        each user's ~/coder/users/<username> folder moves on their first use, and the
        top-level files (single-user mode) move into the global data folder.
        anything that already exists at the new location is left where it is
        """
        if not hasattr(self, "_migrated_sandboxes"):
            self._migrated_sandboxes = set()
        if name != os.path.basename(self.legacy_sandbox_path) or (name, user) in self._migrated_sandboxes:
            return
        self._migrated_sandboxes.add((name, user))

        legacy_root = os.path.expanduser(self.legacy_sandbox_path)
        legacy_users = os.path.join(legacy_root, "users")
        source = core.sandbox_path(legacy_users, user) if user else legacy_root
        if not os.path.isdir(source):
            return

        os.makedirs(root, exist_ok=True)
        for entry in os.listdir(source):
            # other users' folders, they're migrated on their own first use
            if not user and entry == "users":
                continue

            target = os.path.join(root, entry)
            if os.path.exists(target):
                core.log("coder", f"Not migrating {os.path.join(source, entry)}: {target} already exists")
                continue
            try:
                shutil.move(os.path.join(source, entry), target)
            except OSError as e:
                core.log("coder", f"Could not migrate {os.path.join(source, entry)} to {target}: {e}")

        # tidy up the legacy folders once they're empty
        for folder in (source, legacy_users, legacy_root):
            try:
                os.rmdir(folder)
            except OSError:
                pass

    def _user_scoped_path(self, sandbox_root: str):
        """
        in multi-user mode every user gets a private folder inside the sandbox,
        so the AI (and the webui preview, which resolves through here too) only
        ever sees the current user's files. sandbox names stay the same for
        every user, so the AI's prompt and preview urls don't change.
        """
        user = core.current_user.get()
        if not user or not self.config.get("per_user_sandboxes"):
            return sandbox_root

        scoped = core.sandbox_path(sandbox_root, os.path.join("users", user))
        os.makedirs(scoped, exist_ok=True)
        return scoped

    async def _get_sandbox_subpath(self, sandbox: str, requested_path: str):
        """
        resolves a basename'd sandbox path back to its full path,
        then checks whether the requested path is within the sandbox,
        and if so, returns it
        """
        # only remove sandbox prefix if it matches exactly or is followed by a separator
        norm_req = requested_path.replace("\\", "/")
        norm_box = sandbox.replace("\\", "/").strip("/")
        if norm_req == norm_box:
            requested_path = ""
        elif norm_req.startswith(f"{norm_box}/"):
            requested_path = norm_req[len(norm_box) + 1:]
        sandbox_path = await self._get_full_sandbox_path(sandbox)
        return core.sandbox_path(sandbox_path, requested_path)

    # ----------------------------
    # helper functions: site media
    # ----------------------------
    def _sniff_media(self, data: bytes):
        """returns the file extension for supported image/video bytes, else None. never trusts the filename."""
        if not data:
            return None

        kind = filetype.guess(data)
        if kind:
            return self.media_types.get(kind.mime)

        # svg is text, so filetype can't detect it
        head = data[:2048].decode("utf-8", errors="ignore").lstrip("﻿ \t\r\n")
        if head.startswith("<svg") or (head.startswith("<?xml") and "<svg" in head):
            return "svg"
        return None

    def _media_slug(self, name: str, fallback: str = "image"):
        """turns a user/url supplied filename (without extension) into a safe, url-friendly one"""
        slug = regex.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-_")
        return slug[:60] or fallback

    def _unique_media_name(self, folder: str, base: str, ext: str):
        name = f"{base}.{ext}"
        n = 2
        while os.path.exists(os.path.join(folder, name)):
            name = f"{base}-{n}.{ext}"
            n += 1
        return name

    async def _site_media_dir(self, sandbox: str, site_folder: str):
        """resolves (and path-checks) the media folder of the site whose entry html lives in site_folder"""
        site_folder = (site_folder or "").replace("\\", "/").strip("/")
        media_rel = f"{site_folder}/{self.media_folder}" if site_folder else self.media_folder
        return await self._get_sandbox_subpath(sandbox, media_rel)

    def _check_media_size(self, data: bytes):
        max_mb = self.config.get("max_media_size_mb") or 15
        if len(data) > max_mb * 1024 * 1024:
            raise ValueError(f"File is larger than the {max_mb}MB media limit")

    async def _save_site_media(self, sandbox: str, site_folder: str, filename: str, data: bytes):
        """
        saves an image/video into a site's media folder under a safe, unique name.
        returns {"name", "path"} where path is relative to the site folder (what the html uses).
        """
        self._check_media_size(data)
        ext = self._sniff_media(data)
        if not ext:
            raise ValueError(f"'{filename}' is not a supported image or video "
                             f"({', '.join(sorted(set(self.media_types.values())))})")

        media_dir = await self._site_media_dir(sandbox, site_folder)
        os.makedirs(media_dir, exist_ok=True)

        base = self._media_slug(os.path.splitext(os.path.basename(filename or ""))[0])
        # 'x' mode: never overwrite, even if another upload races us for the same name
        while True:
            name = self._unique_media_name(media_dir, base, ext)
            try:
                with open(os.path.join(media_dir, name), "xb") as f:
                    f.write(data)
                break
            except FileExistsError:
                continue

        return {"name": name, "path": f"{self.media_folder}/{name}"}

    async def _list_site_media(self, sandbox: str, site_folder: str):
        media_dir = await self._site_media_dir(sandbox, site_folder)
        if not os.path.isdir(media_dir):
            return []

        items = []
        for name in sorted(os.listdir(media_dir), key=str.lower):
            full = os.path.join(media_dir, name)
            if not os.path.isfile(full) or name.startswith("."):
                continue
            items.append({
                "name": name,
                "path": f"{self.media_folder}/{name}",
                "size": os.path.getsize(full),
                "placeholder": name.startswith("placeholder-") and name.endswith(".svg"),
                "video": name.rsplit(".", 1)[-1].lower() in ("mp4", "webm"),
            })
        return items

    async def _media_file_path(self, sandbox: str, site_folder: str, name: str):
        """path of one existing file in a site's media folder. name must be a bare filename."""
        if not name or name != os.path.basename(name.replace("\\", "/")) or name.startswith("."):
            raise ValueError("Invalid media file name")
        media_dir = await self._site_media_dir(sandbox, site_folder)
        full = core.sandbox_path(media_dir, name)
        if not os.path.isfile(full):
            raise FileNotFoundError(f"{name} does not exist in this site's media folder")
        return full

    async def _delete_site_media(self, sandbox: str, site_folder: str, name: str):
        os.remove(await self._media_file_path(sandbox, site_folder, name))

    async def _replace_site_placeholder(self, sandbox: str, site_folder: str, placeholder: str,
                                        filename: str, data: bytes):
        """
        swaps a placeholder for a real file: saves the new file, rewrites every
        `media/<placeholder>` reference in the site's text files to point at it,
        then deletes the placeholder. no AI round trip needed.
        """
        old_path = await self._media_file_path(sandbox, site_folder, placeholder)
        saved = await self._save_site_media(sandbox, site_folder, filename, data)

        site_root = os.path.dirname(os.path.dirname(old_path))
        old_ref = f"{self.media_folder}/{placeholder}"
        new_ref = saved["path"]
        blacklist = set(self.config.get("folder_blacklist") or [])

        updated = []
        for root, folders, files in os.walk(site_root):
            folders[:] = [d for d in folders
                          if not d.startswith(".") and d not in blacklist and d != self.media_folder]
            for file in files:
                if not file.lower().endswith(self.site_text_extensions):
                    continue
                full = os.path.join(root, file)
                try:
                    if os.path.getsize(full) > 2 * 1024 * 1024:
                        continue
                    with open(full, "r", encoding="utf-8") as f:
                        content = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if old_ref not in content:
                    continue
                with open(full, "w", encoding="utf-8") as f:
                    f.write(content.replace(old_ref, new_ref))
                updated.append(os.path.relpath(full, site_root).replace(os.sep, "/"))

        os.remove(old_path)
        return {**saved, "files_updated": updated}

    async def _assert_public_host(self, host: str, port: int):
        """
        refuses hosts that resolve to loopback/private/link-local/etc addresses, so
        a (possibly prompt-injected) import can't be pointed at the user's lan or
        cloud metadata endpoints. note: httpx resolves again when connecting, so a
        dns-rebinding host could still slip through - this blocks the plain cases.
        """
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port)
        except OSError:
            raise ValueError(f"Could not resolve {host}")

        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if getattr(ip, "ipv4_mapped", None):
                ip = ip.ipv4_mapped
            if not ip.is_global or ip.is_multicast:
                raise ValueError("Importing from local or private network addresses is not allowed")

    async def _download_media(self, url: str):
        """downloads a url with ssrf checks on every redirect hop and a size cap. returns (bytes, filename)."""
        import httpx

        max_bytes = (self.config.get("max_media_size_mb") or 15) * 1024 * 1024

        for _ in range(5):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("Only http:// and https:// URLs can be imported")
            await self._assert_public_host(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))

            async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
                async with client.stream("GET", url, headers={"User-Agent": "OpenLumara media import"}) as resp:
                    if resp.is_redirect:
                        url = urllib.parse.urljoin(url, resp.headers.get("location", ""))
                        continue
                    if resp.status_code >= 400:
                        raise ValueError(f"The server answered with HTTP {resp.status_code}")

                    length = resp.headers.get("content-length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        raise ValueError(f"File is larger than the {max_bytes // 1048576}MB media limit")

                    chunks = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(f"File is larger than the {max_bytes // 1048576}MB media limit")
                        chunks.append(chunk)

                    filename = urllib.parse.unquote(os.path.basename(parts.path)) or "image"
                    return b"".join(chunks), filename

        raise ValueError("Too many redirects")

    async def _import_site_media(self, sandbox: str, site_folder: str, url: str, filename: str = None):
        data, url_filename = await self._download_media(url)
        return await self._save_site_media(sandbox, site_folder, filename or url_filename, data)

    def _placeholder_svg(self, label: str, width: int, height: int):
        font_size = max(12, min(width, height) // 12)
        text = html.escape(f"{label} · {width}×{height}")
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid slice">'
            f'<rect width="100%" height="100%" fill="#e5e7eb"/>'
            f'<path d="M0 0L{width} {height}M{width} 0L0 {height}" stroke="#cbd5e1" stroke-width="2"/>'
            f'<text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" '
            f'font-family="system-ui, sans-serif" font-size="{font_size}" fill="#64748b">{text}</text>'
            f'</svg>'
        )

    # -----------------------------
    # helper functions: treesitter
    # -----------------------------
    async def _check_syntax(self, code: str, file_path: str):
        """verifies code for syntax errors without writing it to disk"""
        errors = []

        if not tslp:
            return []

        lang = tslp.detect_language_from_path(file_path)
        if not lang or lang in ("vimdoc", "html", "htm"):
            # that means treesitter doesn't support the language,
            # or it's a plaintext file (labeled by treesitter as 'vimdoc'),
            # so to avoid blocking code editing for unsupported languages,
            # just pretend the syntax error check passed

            # also, html syntax is skipped because the checker is too aggressive and forbids & characters, causing tons of false positive rejections
            return []

        result = tslp.process(code, tslp.ProcessConfig(language=lang, diagnostics=True, structure=False))
        if not result:
            # was likely an empty file, or failed to parse
            errors.append("Failed to parse the file for syntax errors. For safety, the file was not written to disk.")
            return errors

        if result.diagnostics:
            for diag in result.diagnostics:
                errors.append(f"Line {diag.span.start_line + 1}, Col {diag.span.start_column + 1}: {diag.message}")

        # return errors if they were found, otherwise it's an empty list and callers will know there were no errors
        return errors

    async def _extract_code_structure(self, code: str, file_path: str):
        """
        uses treesitter-language-pack to extract useful information from source code
        this can then be used by the LLM to do very targeted reads of specific sections of code,
        reducing the need to read entire files into context and thus saving on token use massively

        (..but it's not done yet.)
        """
        lang = tslp.detect_language_from_path(file_path)
        if not lang:
            # return None so that the caller can handle it and tell the user
            return None
        if not tslp:
            return None

        lang = tslp.detect_language_from_path(file_path)
        extraction = tslp.process(code, tslp.ProcessConfig(
            language=lang,
            structure=True,
            imports=True,
            comments=False
        ))
        if not extraction:
            # was likely an empty file, or failed to parse
            return None

        data = {}
        data["symbols"] = []
        for item in extraction.structure:
            data["symbols"].append({
                "name": str(item.name),
                "kind": str(item.kind),
                "start_line": int(item.span.start_line),
                "end_line": int(item.span.end_line),
                "doc_comment": item.doc_comment,
                "signature": item.signature
            })

        if not data["symbols"]:
            # tell the AI that this is a file that must be read manually instead
            return None

        metrics = extraction.metrics
        data["metrics"] = {
            "total_lines": metrics.total_lines,
            "error_count": metrics.error_count
        }
        data["imports"] = [imprt.source for imprt in extraction.imports]

        return data

    # ---------------------------
    # helper functions: templates
    # ---------------------------
    async def _get_template_folders(self):
        template_folders = list(self.config.get("template_paths", default=[]))

        # add the internal templates path to the list of template folders
        if self.config.get("enable_builtin_templates"):
            template_folders.insert(0, self.builtin_templates_path)

        return template_folders or []

    async def _get_templates(self):
        all_templates = []
        template_folders = await self._get_template_folders()

        if not template_folders:
            return []

        for folder in template_folders:
            # resolve relative paths to the openlumara root,
            # absolute paths to their absolute locations
            folder_path = core.get_path(folder)

            try:
                templates = os.listdir(folder_path)
            except Exception as e:
                return self.result(str(e), success=False)

            all_templates.extend(templates)

        return all_templates

    # ----------------------
    # tools: file navigation
    # ----------------------
    async def list_sandboxes(self):
        return self.result(await self._get_sandbox_paths())

    async def glob(self, sandbox: str, pattern: str, sub_path=None, recursive: bool = False, max_results: int = 200):
        """globs path for desired files. does not support regex. paths relative to sandbox root."""
        sandbox_path = await self._get_full_sandbox_path(sandbox)
        target_path = await self._get_sandbox_subpath(sandbox, sub_path or '.')

        folder_blacklist = self.config.get("folder_blacklist")

        try:
            # remove the sandbox path itself from the string in case the AI decided to add it
            if pattern.startswith(sandbox):
                pattern = pattern[len(sandbox):]

            # if the pattern doesn't contain **, prefix with **/ for recursive matching
            if recursive:
                if '**' not in pattern and '/' not in pattern:
                    pattern = f"**/{pattern}"
                elif '/' in pattern and '**' not in pattern:
                    pattern = f"**/{pattern}"

            matches = sorted(glob.glob(
                os.path.join(target_path, pattern),
                recursive=recursive
            ))

            results = []
            total = 0
            for match in matches:
                rel_path = os.path.relpath(match, target_path)

                # skip if any path component is in the blacklist
                path_parts = rel_path.split(os.sep)
                if any(part in folder_blacklist for part in path_parts):
                    continue

                total += 1
                if total > int(max_results):
                    break

                # ensure it's within the sandbox
                core.sandbox_path(sandbox_path, rel_path)
                results.append(rel_path)

            if total > int(max_results):
                return self.result({
                    "results": results,
                    "truncated": True,
                    "total": total,
                    "note": f"showing first {int(max_results)} of {total} matches - narrow your pattern to see more"
                })
            return self.result(results)
        except Exception as e:
            return self.result(str(e), success=False)

    async def folder_grep(self, sandbox: str, sub_path: str, regex_pattern: str, case_sensitive=False, context: int = 0, file_extensions: list = None, max_matches: int = 30):
        """searches for regex pattern in all files in folder"""
        folder_blacklist = self.config.get("folder_blacklist")

        if regex_pattern.strip() in [".", "*"]:
            return self.result("Forbidden pattern detected. Use a more specific search!", success=False)

        # compile regex with optional case-insensitive flag
        flags = 0 if case_sensitive else regex.IGNORECASE
        compiled_pattern = regex.compile(regex_pattern, flags)

        target_path = await self._get_sandbox_subpath(sandbox, sub_path)
        if os.path.isdir(target_path):
            # recursive search
            matches = []
            for (recursive_path, folders, files) in os.walk(target_path):
                # Filter out blacklisted and hidden directories using config
                folders[:] = [d for d in folders if not d.startswith('.') and d not in folder_blacklist]

                if len(matches) >= int(max_matches):
                    break

                for file in files:
                    try:
                        filepath = os.path.join(recursive_path, file)
                        
                        # filter by extensions if specified
                        if file_extensions:
                            _, ext = os.path.splitext(file)
                            if ext[1:] not in file_extensions:
                                continue

                        with open(filepath, 'r', encoding="utf-8") as f:
                            all_lines = f.readlines()
                            for line_num, line in enumerate(all_lines, 1):
                                if compiled_pattern.search(line, timeout=self.regex_timeout):
                                    match_dict = {
                                        "file": os.path.relpath(filepath, target_path),
                                        "line_num": line_num,
                                        "line": line.rstrip(),
                                    }
                                    if context:
                                        c_num = int(context)
                                        match_dict.update({
                                            "context_before": [all_lines[i].rstrip() for i in range(max(0, line_num - 1 - c_num), line_num - 1)],
                                            "context_after": [all_lines[i].rstrip() for i in range(line_num, min(len(all_lines), line_num + c_num))] if c_num > 0 else []
                                        })
                                    matches.append(match_dict)
                                    if len(matches) >= int(max_matches):
                                        return self.result({
                                            "matches": matches,
                                            "note": f"Showing up to {int(max_matches)} matches. More results may be available — narrow your search pattern to see them."
                                        })
                    except Exception as e:
                        pass

            if matches:
                return self.result(matches)
            else:
                return self.result("No matches found", success=False)
        else:
            return self.result("Target path is not a folder. Grep only works on folders.", success=False)


    # ----------------------
    # tools: file management
    # ----------------------
    async def file_create(self, sandbox: str, path: str, content: str):
        """creates new file. auto-creates parent dirs in path"""
        target_path = await self._get_sandbox_subpath(sandbox, path)

        # first, check for syntax errors
        syntax_errors = await self._check_syntax(content, target_path)
        if syntax_errors:
            return self.result({"errors": syntax_errors, "message": "Syntax errors detected! File was not written to disk."}, success=False)

        # auto-create any parent folders that don't exist yet,
        # so the AI doesn't have to bother creating them manually
        parent_dir = os.path.dirname(target_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # using try/except here instead of checking if the file exists, to defend against
        # TOCTOU (Time-Of-Check -> Time-Of-Use)
        # a race condition vulnerability that can escape sandboxes
        # am i the only one that thinks TOCTOU kinda sounds like TOC TUA aka HAWK TUAH? oh no
        try:
            # explicit utf-8: the platform default (cp1252 on Windows) would break
            # file_read/file_edit, which both read back with encoding="utf-8"
            with open(target_path, 'x', encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return self.result(str(e), success=False)

        return self.result(f"File {path} successfully created")

    async def file_move(self, sandbox: str, orig_path: str, target_path: str):
        orig_path_sandboxed = await self._get_sandbox_subpath(sandbox, orig_path)
        target_path_sandboxed = await self._get_sandbox_subpath(sandbox, target_path)

        try:
            shutil.move(orig_path_sandboxed, target_path_sandboxed)
        except Exception as e:
            # TAWK TUAH
            return self.result(str(e), success=False)

        return self.result(f"File successfully moved: {orig_path_sandboxed} -> {target_path_sandboxed}")

    async def file_delete(self, sandbox: str, path: str):
        """only use this if user explicitely requests it"""
        target_path = await self._get_sandbox_subpath(sandbox, path)

        try:
            os.remove(target_path)
        except Exception as e:
            # TAWCC THUA
            return self.result(str(e), success=False)

        return self.result(f"File {path} successfully deleted")

    async def folder_delete(self, sandbox: str, path: str):
        """only use this if user explicitely requests it. can only remove empty folders as safety precaution."""
        target_path = await self._get_sandbox_subpath(sandbox, path)
        
        try:
            os.rmdir(target_path)
        except Exception as e:
            # FAWK SHUA
            return self.result(str(e), success=False)

        return self.result(f"Folder {path} deleted")

    # ----------------------
    # tools: file reading
    # ----------------------
    async def file_outline(self, sandbox: str, path: str):
        """provides information about source code. only works on source code files."""
        target_path = await self._get_sandbox_subpath(sandbox, path)
        try:
            with open(target_path, 'r', encoding="utf-8") as f:
                file_content = f.read()
                structure = await self._extract_code_structure(file_content, target_path)
                if not structure:
                    return self.result("This file could not be read by the code inspector because it is not a source code file. Use file_read instead", success=False)

                return self.result(structure)
        except Exception as e:
            # FAWK KUAH
            return self.result(str(e), success=False)

    async def file_read(self, sandbox: str, path: str, line_start: int = None, line_end: int = None):
        """reads file, or part of file. use line_start and line_end to read in chunks."""
        target_path = await self._get_sandbox_subpath(sandbox, path)

        # protect against tocccc touh
        try:
            content = None
            with open(target_path, 'r', encoding="utf-8") as f:
                content = f.read()

            if not content:
                return self.result("File was empty", success=False)
        except Exception as e:
            return self.result(str(e), success=False)

        content_lines = content.split("\n")
        total_lines = len(content_lines)

        max_lines = self.config.get("global_line_limit")

        # filesize gate
        if line_start is None and line_end is None:
            if total_lines <= max_lines:
                # Allow plain reads for small files
                line_start = 1
                line_end = total_lines
            else:
                return self.result(f"File is too large to read in full! ({total_lines} lines). Read it in chunks.", success=False)

        # default ranges
        if line_start is None:
            line_start = 1
        if line_end is None:
            line_end = -1

        # make sure it's 0-indexed
        line_start = line_start-1

        # clamp line start to above 1
        if line_start < 0:
            line_start = 1

        # change a line end of -1 to the actual last line
        if line_end == -1 or line_end > total_lines:
            line_end = total_lines
        elif line_end < 1:
            line_end = 1

        # enforce hard limit on chunk size
        hit_line_limit = line_end - line_start > max_lines
        if hit_line_limit:
            line_end = line_start + max_lines

        # now get only the requested part
        content_partial = content_lines[line_start:line_end]

        # tell the AI what got truncated
        remaining = total_lines - line_end
        truncated = remaining > 0 or hit_line_limit
        truncation_note = f"Showing lines {line_start+1}-{line_end} of {total_lines}. {remaining} lines remaining." if truncated else None
        if hit_line_limit:
            truncation_note += f" WARNING: request clamped to global line limit of {max_lines} lines"

        result = {"content": "\n".join(content_partial), "truncated": truncated}
        if truncated:
            result["truncation_note"] = truncation_note
        result["success"] = True

        return result

    async def file_grep(self, sandbox: str, file_path: str, regex_pattern: str, case_sensitive: bool = True, context: int = 0, max_matches: int = 30):
        target_path = await self._get_sandbox_subpath(sandbox, file_path)
        
        # compile regex with optional case-insensitive flag
        flags = 0 if case_sensitive else regex.IGNORECASE
        compiled_pattern = regex.compile(regex_pattern, flags)

        try:
            matches = []
            with open(target_path, 'r', encoding="utf-8") as f:
                all_lines = f.readlines()
                for line_num, line in enumerate(all_lines, 1):
                    if compiled_pattern.search(line, timeout=self.regex_timeout):
                        match_dict = {
                            "line_num": line_num,
                            "line": line.rstrip()
                        }

                        if context:
                            c_num = int(context)
                            match_dict.update({
                                "context_before": [all_lines[i].rstrip() for i in range(max(0, line_num - 1 - c_num), line_num - 1)],
                                "context_after": [all_lines[i].rstrip() for i in range(line_num, min(len(all_lines), line_num + c_num))] if c_num > 0 else []
                            })
                        matches.append(match_dict)

                        if len(matches) >= int(max_matches):
                            return self.result({
                                "matches": matches,
                                "note": f"Showing up to {int(max_matches)} matches. More results may be available — narrow your search pattern to see them."
                            })

            if matches:
                return self.result(matches)
            else:
                return self.result("No matches found", success=False)
        except Exception as e:
            # FKAWK SUAH
            return self.result(str(e), success=False)

    async def read_template(self, template_name: str):
        template_folders = await self._get_template_folders()
        if not template_folders:
            raise Exception("no template folders were configured! this should never happen, since the builtin one is always included. notify the developer!")

        for folder in template_folders:
            # resolve relative paths to the openlumara root,
            # absolute paths to their absolute locations
            folder_path = core.get_path(folder)

            try:
                templates = os.listdir(folder_path)
            except Exception as e:
                # LAWK FUAH
                return self.result(str(e), success=False)

            if template_name in templates:
                try:
                    with open(os.path.join(folder_path, template_name), 'r', encoding="utf-8") as f:
                        return self.result(f.read())
                except Exception as e:
                    # FAWK MUAH
                    return self.result(str(e), success=False)

        return self.result("Template not found!", success=False) 

    # --------------------
    # tools: file writing
    # --------------------
    async def file_edit(self, sandbox: str, path: str, original_code: str, replacement_code: str):
       target_path = await self._get_sandbox_subpath(sandbox, path)

       file_content = None
       # protect against FAWK HUAH
       try:
           with open(target_path, 'r', encoding="utf-8") as f:
               file_content = f.read()
       except Exception as e:
           return self.result(str(e), success=False)

       if not file_content:
           return self.result("File is empty!", success=False)

       # now do the actual replacement
       new_file_content = file_content.replace(original_code, replacement_code)

       if new_file_content == file_content:
           # that means it failed to replace!
           return self.result("Failed to match your original code string against the file contents. Try again!", success=False)

       # check for syntax errors
       syntax_errors = await self._check_syntax(new_file_content, target_path)
       if syntax_errors:
           return self.result({"errors": syntax_errors, "message": "Syntax errors detected! File was not written to disk."}, success=False)

       # and write it to the file
       try:
           with open(target_path, 'w', encoding="utf-8") as f:
               f.write(new_file_content)
       except Exception as e:
           # TAWK TOUHA
           return self.result(str(e), success=False)

       return self.result(f"Successfully edited file {path}")

    # ----------------------
    # tools: webui preview
    # ----------------------
    async def open_preview(self, sandbox: str, path: str):
        """Opens the given file in the WebUI's live preview panel. Call this after creating or
        editing an HTML file (or site) the user should see. For multi-page sites, use the entry
        HTML file (e.g. index.html). The user can then tell you to change things in plain words."""
        target_path = await self._get_sandbox_subpath(sandbox, path)
        if not os.path.isfile(target_path):
            return self.result("That file does not exist yet. Create it first, then call open_preview.", success=False)
        return self.result({"preview": {"sandbox": sandbox, "path": path}})

    async def media_placeholder(self, sandbox: str, site_folder: str, name: str, width: int = 1200,
                                height: int = 800, label: str = None):
        """Creates a placeholder image (SVG) in the site's media folder for an image the user hasn't provided
        yet, e.g. name 'hero' or 'team-1'. site_folder is the folder holding the site's entry HTML file,
        relative to the sandbox ('' for the sandbox root). Use the returned path (relative to site_folder)
        in the HTML/CSS. The user can later swap it for a real photo from the preview panel."""
        width = max(16, min(int(width), 4000))
        height = max(16, min(int(height), 4000))
        slug = self._media_slug(name, fallback="image")
        filename = f"placeholder-{slug}.svg"

        try:
            media_dir = await self._site_media_dir(sandbox, site_folder)
            os.makedirs(media_dir, exist_ok=True)
            with open(core.sandbox_path(media_dir, filename), "w", encoding="utf-8") as f:
                f.write(self._placeholder_svg(label or slug.replace("-", " ").title(), width, height))
        except Exception as e:
            return self.result(str(e), success=False)

        return self.result({"path": f"{self.media_folder}/{filename}"})

    async def media_import_url(self, sandbox: str, site_folder: str, url: str, filename: str = None):
        """Downloads an image or video from a public http(s) URL into the site's media folder, so the site
        doesn't hotlink it. site_folder is the folder holding the site's entry HTML file, relative to the
        sandbox ('' for the sandbox root). Optional filename (without extension) names the saved file.
        Use the returned path (relative to site_folder) in the HTML/CSS."""
        try:
            saved = await self._import_site_media(sandbox, site_folder, url, filename)
        except Exception as e:
            return self.result(str(e), success=False)
        return self.result({"path": saved["path"]})

    # ---------------------
    # user-facing commands
    # ---------------------
    @core.module.command("coder", help={
       "readonly": "toggle read-only mode"
    })
    async def cmd_coder(self, args: list):
        if len(args) == 0:
            return "please specify a sub-command (see /help coder)"

        match args[0]:
            case "readonly":
                current_state = self.config.get("read-only")
                self.config.set("read-only", not current_state)
                await self.manager.reload_module(self.name)

                if current_state:
                    return "read-only mode disabled"
                else:
                    return "read-only mode enabled"
