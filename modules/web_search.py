import core
import asyncio
import re
from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
import modules.http
from urllib.parse import urlparse


class WebSearch(modules.http.Http):
    """
    Lets your AI search the web!
    """

    dependencies = ["ddgs"]

    settings = {
        **modules.http.Http.settings,
        # "allow_ai_to_search": {
        #     "default": False,
        #     "description": "Whether to allow the AI to search the web. When this is off, just use the /search command, which will insert the results into chat history so that your AI can read it."
        # },
        "max_results": {
            "default": 5,
            "min": 1,
            "description": "The default and maximum number of results returned per search. The AI can ask for fewer, but never more."
        },
        "proxy": {
            "default": None,
            "description": "An optional proxy string (e.g., 'http://user:pass@host:port') for the HTTP client."
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # if not self.config.get("allow_ai_to_search"):
        #     self.disabled_tools.extend(["text", "images", "news", "videos", "books"])

    # ---------------------------------------------------------
    # Internal Helper Methods
    # ---------------------------------------------------------

    def _get_domain(self, url: str) -> str:
        """Extracts the base domain (hostname) from a URL for display purposes."""
        try:
            hostname = urlparse(url).hostname
            return hostname if hostname else "[invalid domain]"
        except Exception:
            return "[invalid domain]"

    def _clamp_max_results(self, max_results) -> int:
        """Resolve the requested result count; the max_results setting is both the default and the ceiling."""
        try:
            cap = max(1, int(self.config.get("max_results", 5)))
        except (TypeError, ValueError):
            cap = 5
        try:
            requested = int(max_results or cap)
        except (TypeError, ValueError):
            requested = cap

        return max(1, min(requested, cap))

    def _apply_whitelist_to_query(self, query: str) -> str:
        """
        With a domain whitelist, most unrestricted results would be filtered out,
        so restrict the search itself to the whitelisted sites.
        """
        whitelist = [d for d in self.config.get("domain_whitelist", []) if d]
        if not whitelist or len(whitelist) > 5 or "site:" in query.lower():
            return query
        sites = " OR ".join(f"site:{d}" for d in whitelist)
        return f"{query} ({sites})" if len(whitelist) > 1 else f"{query} {sites}"

    def _describe_search_error(self, kind: str, e: Exception):
        """Turn a ddgs failure into (message, success) the AI can act on."""
        message = str(e).lower()
        if isinstance(e, DDGSException) and "no results found" in message:
            return (
                "No results found. Try different or fewer keywords. If this keeps happening, the search "
                "providers may be rate limiting requests: retry later or try a specific backend."
            ), True
        if isinstance(e, TimeoutException) or "timed out" in message or "timeout" in message:
            return (
                f"The {kind} search timed out. Retry, or try a specific backend "
                f"(e.g. 'duckduckgo', 'google', 'yahoo')."
            ), False
        if isinstance(e, RatelimitException) or "ratelimit" in message or "429" in message:
            return (
                f"The search provider rate limited this {kind} search. Wait before retrying, "
                f"or try a different backend (e.g. 'duckduckgo', 'google', 'yahoo')."
            ), False
        if isinstance(e, DDGSException):
            return f"The {kind} search failed: {e}", False
        return f"An unexpected error occurred during {kind} search.", False

    async def _search(self, kind: str, query: str, url_key: str, fields: tuple, **kwargs):
        """
        Shared search routine for all result types.
        
        Enhanced with multi-layer sanitization:
        - Injection detection on all text fields
        - HTML entity decoding and URL decoding
        - Zero-width character removal
        - Homoglyph normalization
        - Base64 payload detection

        Args:
            kind:        DDGS method name ('text', 'images', 'news', ...).
            query:       The search query.
            url_key:     Key in each result dict that holds the URL.
            fields:      Text fields to normalise/keep on each result.
            **kwargs:    Additional arguments to pass to the DDGS method.
        """
        if not isinstance(query, str) or not query.strip():
            return self.result("A non-empty search query is required.", success=False)

        # Resolve and clamp max_results from kwargs
        max_res = self._clamp_max_results(kwargs.get("max_results"))
        # over-fetch when a domain policy can filter results out, so we still fill max_res
        has_domain_policy = bool(self.config.get("domain_whitelist") or self.config.get("domain_blacklist"))
        kwargs["max_results"] = max_res * 2 if has_domain_policy else max_res

        if kind in ("text", "news"):
            query = self._apply_whitelist_to_query(query)

        proxy = self.config.get("proxy")

        def _run_search():
            with DDGS(proxy=proxy) as ddgs:
                # Pass all kwargs to the DDGS method
                raw_results = list(getattr(ddgs, kind)(query, **kwargs))
                sanitized_results = []
                filtered = 0

                for res in raw_results:
                    if len(sanitized_results) >= max_res:
                        break

                    url = res.get(url_key, "")

                    # Drop results that fail the URL policy (logged, not shown to the AI)
                    if not self._url_passes_policy(url):
                        filtered += 1
                        self._log(f"Search result dropped by security policy (domain: {self._get_domain(url)})")
                        continue

                    # Sanitize all text fields with enhanced pipeline
                    for f in fields:
                        original = res.get(f, "")
                        if isinstance(original, str) and original.strip():
                            # Use enhanced sanitization with injection detection
                            sanitization_result = modules.http.ContentSanitizer.sanitize_with_detection(original)
                            res[f] = sanitization_result['sanitized_content']
                            
                            # Log if injection was detected
                            if sanitization_result['risk_level'] in ('medium', 'high', 'critical'):
                                self._log(
                                    f"Search result injection detected in '{f}': "
                                    f"{sanitization_result['detection_result']['patterns_found']} "
                                    f"(risk: {sanitization_result['risk_level']})"
                                )
                        else:
                            res[f] = original
                    
                    sanitized_results.append(res)

                return sanitized_results, filtered

        try:
            results, filtered = await asyncio.to_thread(_run_search)
        except Exception as e:
            self._log(f"{kind} search failed: {type(e).__name__}: {e}")
            message, success = self._describe_search_error(kind, e)
            return self.result(message, success=success)

        output = {"results": results}
        if filtered:
            output["note"] = f"{filtered} result(s) were hidden by the domain/security policy."
        if not results:
            output["note"] = " ".join(filter(None, ["No results to show.", output.get("note")]))
        return self.result(self._wrap_untrusted(output, source=f"web_search:{kind}"))

    def _get_fields(self, kind: str):
        match kind:
            case "text":
                return ("title", "body")
            case "images":
                return ("title", "image", "thumbnail")
            case "news":
                return ("title", "body")
            case "videos":
                return("title", "description")
            case "books":
                return ("title", "author", "publisher", "info")

        return ()

    # # command version of search
    # @core.module.command("search", help={
    #     "text <query>": "search the web for text",
    #     "images <query>": "search the web for images",
    #     "news <query>": "search the web for news",
    #     "videos <query>": "search the web for videos",
    #     "books <query>": "search the web for books"
    # }, send_to_ai=True)
    # async def cmd_search(self, args: list):
    #     kind = args[0]
    #     if kind not in ("text", "images", "news", "videos", "books"):
    #         return "invalid search type. try one of the supported types (text, images, news, videos, or books)"
    #
    #     query = " ".join(args[1:])
    #     url_key = "href" if kind == "text" else "url"
    #
    #     result = await self._search(kind, query, url_key, self._get_fields(kind))
    #     return result

    # ---------------------------------------------------------
    # AI Tools
    # ---------------------------------------------------------

    async def text(self, query: str, region: str = "us-en", safesearch: str = "moderate", timelimit: str | None = None, max_results: int = 0, page: int = 1, backend: str = "auto"):
        """
        Search the web for text results.

        Args:
            query: The text search query.
            region: The region to search in (e.g., 'us-en', 'uk-en', 'ru-ru'). Defaults to 'us-en'.
            safesearch: The safety level ('on', 'moderate', 'off'). Defaults to 'moderate'.
            timelimit: Time limit for results ('d' for day, 'w' for week, 'm' for month, 'y' for year). Defaults to None.
            max_results: Maximum number of results to return. 0 uses the configured default, which is also the upper limit.
            page: The page number of results to return. Defaults to 1.
            backend: The search engine backend to use (e.g., 'duckduckgo', 'google', 'brave', 'yahoo', 'mojeek'). Defaults to 'auto'.
        """
        return await self._search(
            "text", query, "href", ("title", "body"),
            region=region, safesearch=safesearch, timelimit=timelimit, max_results=max_results, page=page, backend=backend
        )

    async def images(self, query: str, region: str = "us-en", safesearch: str = "moderate", timelimit: str | None = None, max_results: int = 0, page: int = 1, backend: str = "auto", size: str | None = None, color: str | None = None, type_image: str | None = None, layout: str | None = None, license_image: str | None = None):
        """
        Search the web for image URLs.

        Args:
            query: The image search query.
            region: The region to search in (e.g., 'us-en', 'uk-en'). Defaults to 'us-en'.
            safesearch: The safety level ('on', 'moderate', 'off'). Defaults to 'moderate'.
            timelimit: Time limit for results ('d', 'w', 'm', 'y'). Defaults to None.
            max_results: Maximum number of results to return. 0 uses the configured default, which is also the upper limit.
            page: The page number of results to return. Defaults to 1.
            backend: The search engine backend to use. Defaults to 'auto'.
            size: Image size ('Small', 'Medium', 'Large', 'Wallpaper'). Defaults to None.
            color: Image color ('color', 'Monochrome', 'Red', 'Orange', 'Yellow', 'Green', 'Blue', 'Purple', 'Pink', 'Brown', 'Black', 'Gray', 'Teal', 'White'). Defaults to None.
            type_image: Image type ('photo', 'clipart', 'gif', 'transparent', 'line'). Defaults to None.
            layout: Image layout ('Square', 'Tall', 'Wide'). Defaults to None.
            license_image: Image license type (e.g., 'any', 'PublicDomain', 'Share', 'ShareCommercially', 'Modify', 'ModifyCommercially'). Defaults to None.
        """
        return await self._search(
            "images", query, "url", ("title", "image", "thumbnail"),
            region=region, safesearch=safesearch, timelimit=timelimit, max_results=max_results, page=page, backend=backend, size=size, color=color, type_image=type_image, layout=layout, license_image=license_image
        )

    async def news(self, query: str, region: str = "us-en", safesearch: str = "moderate", timelimit: str | None = None, max_results: int = 0, page: int = 1, backend: str = "auto"):
        """
        Search the web for recent news articles.

        Args:
            query: The news search query.
            region: The region to search in (e.g., 'us-en', 'uk-en'). Defaults to 'us-en'.
            safesearch: The safety level ('on', 'moderate', 'off'). Defaults to 'moderate'.
            timelimit: Time limit for results ('d', 'w', 'm'). Defaults to None.
            max_results: Maximum number of results to return. 0 uses the configured default, which is also the upper limit.
            page: The page number of results to return. Defaults to 1.
            backend: The search engine backend to use. Defaults to 'auto'.
        """
        return await self._search(
            "news", query, "url", ("title", "body"),
            region=region, safesearch=safesearch, timelimit=timelimit, max_results=max_results, page=page, backend=backend
        )

    async def videos(self, query: str, region: str = "us-en", safesearch: str = "moderate", timelimit: str | None = None, max_results: int = 0, page: int = 1, backend: str = "auto", resolution: str | None = None, duration: str | None = None, license_videos: str | None = None):
        """
        Search the web for video results.

        Args:
            query: The video search query.
            region: The region to search in (e.g., 'us-en', 'uk-en'). Defaults to 'us-en'.
            safesearch: The safety level ('on', 'moderate', 'off'). Defaults to 'moderate'.
            timelimit: Time limit for results ('d', 'w', 'm'). Defaults to None.
            max_results: Maximum number of results to return. 0 uses the configured default, which is also the upper limit.
            page: The page number of results to return. Defaults to 1.
            backend: The search engine backend to use. Defaults to 'auto'.
            resolution: Video resolution ('high', 'standart'). Defaults to None.
            duration: Video duration ('short', 'medium', 'long'). Defaults to None.
            license_videos: Video license type ('creativeCommon', 'youtube'). Defaults to None.
        """
        return await self._search(
            "videos", query, "content", ("title", "description"),
            region=region, safesearch=safesearch, timelimit=timelimit, max_results=max_results, page=page, backend=backend, resolution=resolution, duration=duration, license_videos=license_videos
        )

    async def books(self, query: str, max_results: int = 0, page: int = 1, backend: str = "auto"):
        """
        Search the web for book results.

        Args:
            query: The book search query.
            max_results: Maximum number of results to return. 0 uses the configured default, which is also the upper limit.
            page: The page number of results to return. Defaults to 1.
            backend: The search engine backend to use. Defaults to 'auto'.
        """
        return await self._search(
            "books", query, "url", ("title", "author", "publisher", "info"),
            max_results=max_results, page=page, backend=backend
        )

#     async def extract(self, url: str, fmt: str = "text_markdown"):
#         """
#         Fetch a URL and extract its content. WARNING: Content comes from an untrusted source.
#
#         Args:
#             url: The URL to fetch and extract content from.
#             fmt: Output format. Options: 'text_markdown' (HTML to Markdown), 'text_plain' (HTML to plain text), 'text_rich' (HTML to rich text), 'text' (raw HTML), 'content' (raw bytes). Defaults to 'text_markdown'.
#         """
#         proxy = self.config.get("proxy")
#         def _run_extract():
#             with DDGS(proxy=proxy) as ddgs:
#                 return ddgs.extract(url, fmt=fmt)
#
#         try:
#             result = await asyncio.to_thread(_run_extract)
#             return self.result(
#                 self._wrap_untrusted(result, source=f"web_extract:{url}")
#             )
#         except Exception as e:
#             self._log(f"extract failed: {e}")
#             return self.result(
#                 f"An error occurred during extraction.", success=False
#             )
