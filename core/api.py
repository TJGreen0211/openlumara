import core
import httpx
import openai
import asyncio
import json
import os
import time
import inspect
import re

# chat ids that are safe to use as slot cache filenames
_SLOT_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

class APIError:
    """Simple class that holds an error message, used for passing on to channels"""
    def __init__(self, message: str = None, exc = None):
        self.message = message

        # store exception if relevant
        self.exc = None
        if exc:
            self.exc = exc

    def __str__(self):
        err_str = ""
        if self.message:
            err_str += self.message
        if self.exc:
            if self.message:
                err_str += ": "
            err_str += core.detail_error(self.exc)

        return err_str

class APIClient():
    """
    wrapper around the openAI API to make sending/receiving messages easier to work with
    """
    def __init__(self, manager):
        # store a reference to the manager
        self.manager = manager

        self.connected = False
        self._AI = None # replaced later using .connect()
        self.is_streaming = False

        self._messages = []

        self.cancel_request = False
        # self.prompt_warming_up = False
        # self.cancel_prompt_warmup = False
        # self._warmup_task = None
        # self._warmup_queue = asyncio.Queue()
        # self._warmup_done = asyncio.Event()

        # used for insecure SSL connections
        self._httpx_client = None

        self.supports_developer_role = False

        # llama.cpp slot cache (per-chat KV save/restore/erase)
        self._slot_queue = None
        self._slot_worker = None
        # set once the server reports it doesn't support slot actions (eg. started without --slot-save-path)
        self.slot_cache_disabled = False

    async def connect(self, silent=False):
        if self.connected:
            return True

        # any (re)connect might reach a different server build,
        # so drop any earlier "unsupported" verdict before re-checking
        self.slot_cache_disabled = False

        api_config = core.config.get("api", {})

        if api_config.get("url") == "http://API_URL_HERE/v1":
            return APIError("The API connection has not been set up yet! Please set up your API connection by either using the WebUI, the /config command, or editing the config file")

        # infinite timeout
        httpx_timeout = httpx.Timeout(
            connect=5.0,
            read=None,
            write=None,
            pool=None
        )

        use_secure_connection = not self.manager.args.insecure_tls
        if not use_secure_connection:
            self.manager.log("API", "WARNING: TLS certificate and hostname verification are disabled")

        try:
            self._httpx_client = httpx.AsyncClient(
                verify=use_secure_connection,
                timeout=httpx_timeout
            )

            self._AI = openai.AsyncOpenAI(
                base_url=api_config.get("url"),
                api_key=api_config.get("key"),
                http_client=self._httpx_client
            )
            await self._AI.models.list()

        except openai.BadRequestError as e:
            # Check if the error message specifically mentions the model is not found
            error_str = str(e).lower()
            if "model" in error_str and ("not found" in error_str or "missing" in error_str):
                return APIError("Model not found.")
            else:
                # It's a different kind of 400 error (e.g., invalid parameters)
                return APIError(f"Bad request", e)

        except openai.AuthenticationError as e:
            #await self.disconnect()
            return APIError("Authentication failed. Check if your API key is valid.", e)

        except openai.APIConnectionError as e:
            #await self.disconnect()
            return APIError("Failed to connect to the API", e)

        except Exception as e:
            #await self.disconnect()
            import traceback
            if core.debug:
                traceback.print_exc()
            return APIError(None, e)

        # now that the server is reachable, check whether it supports llama.cpp
        # slot actions. this keeps the llama.cpp-only id_slot field out of
        # requests to other OpenAI-compatible backends from the very first message
        if core.config.get("api", "slot_cache", True):
            self.slot_cache_disabled = not await self._probe_slot_support()

        self.connected = True
        self.supports_developer_role = core.config.get("api", "use_developer_role", default=False)

        if not silent:
            self.manager.log("API", "Successfully connected to AI")

        # send the system prompt in the background,
        # so that the AI is ready to respond right away when the user has finished
        # typing their message
        # (thanks to https://www.reddit.com/r/LocalLLaMA/comments/1uskb1g/speculative_cache_warming_warms_your_cache_while/ for the idea)


        # PROMPT WARMING DISABLED FOR NOW (it's extremely buggy and needs a few days of extra polish. it's causing race conditions all over the place)
        #await self.start_prompt_warmup(context=[{"role": "system", "content": await self.manager.get_system_prompt()}], notify=False)

        return True

    async def attempt_connect(self):
        """connect if disconnected, else just return True"""
        if not self.connected:
            return await self.connect()

        return True

    def get_status(self):
        api_config = core.config.get("api", {})
        model_config = core.config.get("model", {})

        return {
            "connected": self.connected,
            "url": api_config.get("url"),
            "model": core.config.get("model", "name")
        }

    # ------------------
    # llama.cpp slot cache (per-chat KV save/restore/erase)
    # ------------------
    def _slot_base_url(self):
        url = core.config.get("api", "url", "") or ""
        if not url or "API_URL_HERE" in url:
            return None

        url = url.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3].rstrip("/")

        return url

    def _slot_enabled(self):
        if self.slot_cache_disabled:
            return False

        if not core.config.get("api", "slot_cache", True):
            return False

        return self.connected and self._httpx_client is not None and self._slot_base_url() is not None

    def _slot_id_configured(self):
        try:
            slot_id = int(core.config.get("api", "slot_id", 0) or 0)
        except (TypeError, ValueError):
            return None

        return slot_id if 0 <= slot_id <= 255 else None

    async def _probe_slot_support(self):
        """check whether the server looks like a llama.cpp build with a reachable
        /slots route. returns True if requests can safely carry the llama.cpp-only
        id_slot field, False otherwise"""
        url = self._slot_base_url()
        if not url or self._httpx_client is None:
            return False

        try:
            response = await self._httpx_client.get(
                f"{url}/slots",
                timeout=httpx.Timeout(timeout=5.0, connect=5.0)
            )
        except Exception:
            self.manager.log("api", "The AI server has no /slots endpoint. Per-chat slot cache is disabled (llama.cpp only)")
            return False

        # a 501 on some llama.cpp builds only means the /slots GET is gated behind
        # the --slots flag; the save/restore/erase POSTs may still work, so keep
        # the feature enabled and let the first real op make the final call
        if response.status_code >= 400 and response.status_code != 501:
            self.manager.log("api", "The AI server has no /slots endpoint. Per-chat slot cache is disabled (llama.cpp only)")
            return False

        return True

    def _enqueue_slot_op(self, action, chat_id):
        """enqueue a slot save/restore/erase op. returns a future that resolves when the op
        finishes (or None if the op was not enqueued)"""
        if not chat_id or not _SLOT_FILENAME_RE.match(chat_id):
            return None

        if not self._slot_enabled():
            return None

        slot_id = self._slot_id_configured()
        if slot_id is None:
            return None

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._ensure_slot_worker().put_nowait((action, chat_id, slot_id, future))
        return future

    def save_chat_cache(self, chat_id):
        """save the current slot's KV cache to the chat's cache file.
        returns a future resolving to True if the server confirmed the save,
        or None if not enqueued"""
        return self._enqueue_slot_op("save", chat_id)

    def restore_chat_cache(self, chat_id):
        """restore the slot's KV cache from the chat's cache file.
        a missing file is a no-op. returns a future (or None) that resolves
        to True once the server confirmed it (or treated it as a no-op)"""
        return self._enqueue_slot_op("restore", chat_id)

    def remove_slot_cache_file(self, chat_id):
        """directly remove the chat's local cache file if api.slot_save_path is set.
        used for deleted chats (some llama.cpp builds keep the file after the
        server's erase action) and for cache files that became stale after the
        served model changed"""
        save_path = core.config.get("api", "slot_save_path", "") or ""
        if not save_path or not chat_id or not _SLOT_FILENAME_RE.match(chat_id):
            return

        try:
            os.remove(os.path.join(save_path, chat_id))
            if core.debug:
                self.manager.log("api", f"removed slot cache file for chat {chat_id}")
        except FileNotFoundError:
            pass
        except Exception as e:
            self.manager.log("api", f"failed to remove slot cache file {chat_id}: {core.detail_error(e)}")

    def erase_chat_cache(self, chat_id):
        """erase the slot's KV cache for the chat and remove its cache file
        locally (if slot_save_path is set). returns a future that resolves when done"""
        self.remove_slot_cache_file(chat_id)
        return self._enqueue_slot_op("erase", chat_id)

    def _ensure_slot_worker(self):
        if self._slot_worker is None or self._slot_worker.done():
            self._slot_queue = asyncio.Queue()
            self._slot_worker = asyncio.create_task(self._slot_worker_loop())

        return self._slot_queue

    async def _slot_worker_loop(self):
        """process slot ops strictly one at a time (FIFO), since the slot is a single
        shared resource. a stale save must never run after a later restore"""
        try:
            while True:
                action, chat_id, slot_id, future = await self._slot_queue.get()

                ok = False
                try:
                    ok = await self._slot_run(action, chat_id, slot_id)
                except Exception:
                    pass
                finally:
                    if not future.done():
                        future.set_result(ok)
        except asyncio.CancelledError:
            # resolve any pending futures before exiting
            # (False: the op never got a chance to be confirmed by the server)
            while not self._slot_queue.empty():
                try:
                    _, _, _, future = self._slot_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if not future.done():
                    future.set_result(False)

            raise

    async def _slot_run(self, action, chat_id, slot_id):
        """perform the op. returns True if the server confirmed it (or if it was an
        expected no-op like restoring a missing cache file), False otherwise"""
        if self._httpx_client is None:
            return False

        url = f"{self._slot_base_url()}/slots/{slot_id}"

        # `action` and `id_slot` come from the query params,
        # but `filename` is read from the JSON body by recent llama.cpp builds
        # (older ones read it from the query params, so send it in both places)
        try:
            response = await self._httpx_client.post(
                url,
                params={"action": action, "filename": chat_id},
                json={"action": action, "filename": chat_id},
                timeout=httpx.Timeout(timeout=60.0, connect=5.0),
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            self.manager.log("api", f"slot {action} for chat {chat_id} failed: {core.detail_error(e)}")
            return False

        status = response.status_code
        if status >= 400:
            if status == 501:
                self.slot_cache_disabled = True
                self.manager.log("api", "The AI server does not support slot cache actions. Start your llama.cpp server with `--slot-save-path` to enable saving and restoring chat contexts (slot cache is now disabled)")
            elif status in (404, 405):
                self.slot_cache_disabled = True
                self.manager.log("api", f"The AI server does not have a /slots endpoint ({status}). Slot cache is now disabled")
            elif action == "restore" and status == 400:
                # no cache file for this chat (or an invalid one) - nothing to restore
                if core.debug:
                    self.manager.log("api", f"no slot cache to restore for chat {chat_id}")
                return True
            else:
                self.manager.log("api", f"slot {action} for chat {chat_id} failed with status {status}: {response.text[:200]}")
            return False

        if core.debug:
            self.manager.log("api", f"slot {action} for chat {chat_id} ok: {response.text[:200]}")

        return True

    async def close_slot_cache(self):
        """stop the slot cache worker and drop any pending ops"""
        worker = self._slot_worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        # resolve any ops that never got a chance to run
        # (a worker cancelled before its first step skips its own cleanup).
        # False: the op was never confirmed by the server
        if self._slot_queue is not None:
            while not self._slot_queue.empty():
                try:
                    _, _, _, future = self._slot_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if not future.done():
                    future.set_result(False)

        self._slot_worker = None
        self._slot_queue = None

    async def disconnect(self):
        """disconnect from the API"""
        await self.close_slot_cache()

        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None

        self.connected = False
        self._AI = None
        return True

    async def reconnect(self):
        """disconnect and reconnect to the API"""
        await self.disconnect()
        return await self.connect()

    def get_model(self):
        return core.config.get("model", "name")

    def set_model(self, name: str):
        core.config.config["model"]["name"] = name
        core.config.set_user_or_global(["model", "name"], name)
        return True

    async def _request(self, context, tools=None, stream=False, use_thinking=True, **kwargs):
        """send a request to the LLM and return the response object"""

        if not context:
            # this should never happen..
            # so if it does, always print a traceback, since it's bad news!
            import traceback
            traceback.print_stack()

            return APIError("Tried to send a blank request for some reason! This should NEVER happen. Notify the developer.")

        connected = await self.attempt_connect()
        if connected is not True:
            # thats an error
            return connected

        if not core.config.get("model", {}).get("use_tools"):
            # allow switching tools off globally
            tools = None

        req = {
            "model": core.config.get("model", "name"),
            "messages": context,
            "tools": tools,
            "stream": stream,
            "temperature": core.config.get("model", {}).get("temperature", 0.2),
            "max_completion_tokens": core.config.get("api", {}).get("max_output_tokens", 8192),
            "extra_body": {
                "chat_template_kwargs": {
                    "enable_thinking": core.config.get("model", "enable_thinking", default=use_thinking)
                },
                "return_progress": True
            }
        }

        # pin requests to a single llama.cpp slot so that the per-chat slot
        # caches (save/restore/erase) are actually used. only done while the
        # slot cache feature is enabled and the server is known to support it.
        if core.config.get("api", "slot_cache", True) and not self.slot_cache_disabled:
            slot_id = self._slot_id_configured()
            if slot_id is not None:
                req["extra_body"]["id_slot"] = slot_id

        # add kwargs to the request
        for key, value in kwargs.items():
            if key in ("tools", "stream", "use_thinking"): continue
            req[key] = value

        reasoning_effort = core.config.get("model", {}).get("reasoning_effort")
        if reasoning_effort != "none":
            req["reasoning_effort"] = reasoning_effort

        # allow inserting custom request fields
        custom_fields = core.config.get("api", {}).get("custom_fields", {})
        if isinstance(custom_fields, dict):
            for key, value in custom_fields.items():
                req[key] = value

        if stream:
            # request token usage from the API
            req["stream_options"] = {"include_usage": True}

        if core.debug:
            message_summary = []
            api_config = core.config.get("api", {})

            for message in context:
                summary = {
                    "role": message.get("role")
                }

                content = message.get("content")
                if isinstance(content, str):
                    summary["content_chars"] = len(content)
                elif isinstance(content, list):
                    summary["content_items"] = len(content)

                if message.get("tool_calls"):
                    summary["tool_calls"] = len(message.get("tool_calls") or [])

                message_summary.append(summary)

            tool_count = len(tools or [])
            custom_field_keys = sorted(list(custom_fields.keys())) if isinstance(custom_fields, dict) else []

            self.manager.log(
                "debug:request",
                json.dumps({
                    "base_url": api_config.get("url"),
                    "model": core.config.get("model", "name"),
                    "stream": stream,
                    "use_thinking": use_thinking,
                    "message_count": len(context),
                    "tool_count": tool_count,
                    "max_completion_tokens": req.get("max_completion_tokens"),
                    "temperature": req.get("temperature"),
                    "reasoning_effort": req.get("reasoning_effort"),
                    "custom_field_keys": custom_field_keys,
                    "messages": message_summary,
                }, ensure_ascii=True, sort_keys=True)
            )

        response = None
        try:
            # if at this point a cancel was already requested,
            # it was likely from a toolcalling chain, so abort EVERYTHING
            #if self.cancel_request and not self.prompt_warming_up:
            if self.cancel_request:
                raise asyncio.CancelledError("Request cancelled")

            request_task = asyncio.create_task(self._AI.chat.completions.create(**req))

            # wrap the request in a way that we can check for cancellation
            # since openai's async client doesn't natively support an abort signal
            # easily through the high-level chat.completions.create, we use a task
            # so we can actually cancel the task itself.

            while not request_task.done():
                if self.cancel_request:
                    request_task.cancel()
                await asyncio.sleep(0.1)

            try:
                response = await request_task
            except asyncio.CancelledError:
                self.cancel_request = False
                raise asyncio.CancelledError("Request cancelled")

        except asyncio.CancelledError as e:
            # fully kill the connection because ive been debuggging this for like 5 hours and im tired
            # make it stop
            #self.manager.log("api", "Force closing HTTP connection due to unclean state..")
            #await self.disconnect()
            self.cancel_request = False

            # and propagate it up for any other stuff to handle
            raise

        except openai.BadRequestError as e:
            # Check if the error message specifically mentions the model is not found
            error_str = str(e).lower()
            if "model" in error_str and ("not found" in error_str or "missing" in error_str):
                return APIError("Model with that name does not exist!", e)
            else:
                # It's a different kind of 400 error (e.g., invalid parameters)
                return APIError("Bad request", e)

        except openai.AuthenticationError as e:
            #await self.disconnect()
            return APIError("Authentication failed. Check whether your API key is valid!", e)

        except openai.APIConnectionError as e:
            #await self.disconnect()
            return APIError("Failed to connect to API")

        except openai.NotFoundError as e:
            return APIError("Model with that name does not exist!", e)

        except openai.RateLimitError as e:
            return APIError("Rate limit exceeded", e)

        except openai.APIStatusError as e:
            return APIError("API Status Error",  e)

        except Exception as e:
            #await self.disconnect()
            return APIError(None, e)

        finally:
            self.cancel_request = False

        if core.debug:
            self.manager.log("debug:response", str(response))

        return response

    async def stop_prompt_warmup(self):
        if self._warmup_task and not self._warmup_task.done():
            self.cancel_prompt_warmup = True
            self._warmup_task.cancel()
            try:
                await self._warmup_task
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.manager.log_error("Warmup task failed", e)
            finally:
                self.cancel_prompt_warmup = False

        # clear the queue completely
        while not self._warmup_queue.empty():
            try:
                self._warmup_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._warmup_task = None
        self.prompt_warming_up = False

    async def start_prompt_warmup(self, context=None, notify=True):
        # cancel existing warmup task if there's already one running
        # (for example if the warmup is running for one chat,
        # and the user switches to a different one)
        await self.stop_prompt_warmup()
        self._warmup_done.clear()

        self._warmup_task = asyncio.create_task(self._run_warmup(context=context, notify=notify))
        if notify:
            self.manager.log("API", "Sending prompt in advance to make AI response instant.. (prompt warmup)")

    async def _run_warmup(self, context=None, notify=True):
        self._warmup_done.clear()
        self.prompt_warming_up = True

        try:
            if context is None:
                prompt = await self.manager.get_system_prompt()
                context = [{"role": "system", "content": prompt}]

            response = await self._request(context, stream=True, tools=self.manager.tools, use_thinking=False, max_completion_tokens=1)

            if isinstance(response, APIError):
                self.manager.log("api", f"Failure while sending prompt warmup request to AI: {response}")
                # thats an error
                return

        except Exception as e:
            self.manager.log("api", f"Failure while sending prompt warmup request to AI: {core.detail_error(e)}")

        try:
            async for token in self._recv_stream(response):
                if self.cancel_request:
                    raise asyncio.CancelledError("Warmup task cancelled")

                if token.get("type") == "prompt_progress":
                    await self._warmup_queue.put(token)
            if notify:
                self.manager.log("API", "Prompt warmup complete")

        except asyncio.CancelledError:
            # fully kill the connection because ive been debuggging this for like 5 hours and im tired
            # make it stop
            #self.manager.log("api", "Force closing HTTP connection due to unclean state..")
            #await self.disconnect()
            self.manager.log("api", "Request was cancelled")
        except Exception as e:
            if notify:
                self.manager.log("api", f"Warmup request failed: {core.detail_error(e)}")
        finally:
            self.prompt_warming_up = False
            self._warmup_done.set()

    async def send(self, context: list, system_prompt=True, use_tools=True, tools=None, use_thinking=True, **kwargs):
        """send a message to the LLM. returns a string or APIError"""

        self.cancel_request = False

        # attempt auto-reconnect once
        connected = await self.attempt_connect()
        if connected is not True:
            # thats an error!
            return connected

        # wait for the system prompt warmup to finish if it's still running
        # if self._warmup_task and not self._warmup_task.done():
        #     if core.debug:
        #         self.manager.log("API", "Waiting for prompt warmup to complete..")
        #     await self._warmup_task

        # use default tools if not specified. allow overrides
        if not tools:
            tools = self.manager.tools

        response = await self._request(context, tools=(tools if use_tools else None), use_thinking=use_thinking, **kwargs)

        # return errors if applicable
        if isinstance(response, APIError):
            return response

        try:
            result = await self._recv(response)
            return result
        except Exception as e:
            return APIError("While processing response from AI", e)

    async def send_stream(self, context: list, use_tools=True, tools=None, use_thinking=True, **kwargs):
        """send a message to the LLM. is an iterable async generator"""

        self.cancel_request = False

        # attempt auto-reconnect once
        connected = await self.attempt_connect()
        if connected is not True:
            # that's an error
            yield {"type": "error", "content": str(connected)}
            return

        # drain progress tokens while waiting for warmup to finish
        # DISABLED DUE TO INTRODUCING A MYRIAD OF BUGS (see my other comments in connect())

        # so that warmup progress can be shown in channels
        # if self._warmup_task and not self._warmup_task.done():
        #     while not self._warmup_done.is_set():
        #         try:
        #             token = self._warmup_queue.get_nowait()
        #             yield token
        #         except asyncio.QueueEmpty:
        #             await asyncio.sleep(0.01)

        # drain any remaining tokens that arrived while we were yielding
        # while not self._warmup_queue.empty():
        #     yield await self._warmup_queue.get()

        # wait for the prompt warmup to actually finish
        # if self._warmup_task and not self._warmup_task.done():
        #     await self._warmup_task

        # use default tools if not specified. allow overrides
        if not tools:
            tools = self.manager.tools

        response = await self._request(context, tools=(tools if use_tools else None), stream=True, use_thinking=use_thinking, **kwargs)

        # return errors if applicable
        if isinstance(response, APIError):
            yield {"type": "error", "content": str(response)}
            return

        try:
            self.is_streaming = True

            async for token in self._recv_stream(response):
                if self.cancel_request:
                    # cancel the entire stream
                    raise asyncio.CancelledError("Request cancelled")

                if core.debug_stream:
                    self.manager.log("debug:stream", json.dumps(token, ensure_ascii=True))

                # let the channel calling send_stream() handle token processing
                yield token
        except asyncio.CancelledError:
            self.cancel_request = False
            raise  # let callers handle cancellation
        except Exception as e:
            yield {"type": "error", "content": f"While sending request to AI: {core.detail_error(e)}"}
        finally:
            self.is_streaming = False

    async def cancel(self):
        """cancel a request that's been sent to the AI"""
        if not self.is_streaming:
            return False

        self.cancel_request = True

        # wait for the cancellation to complete
        while self.cancel_request:
            await asyncio.sleep(0.05)

        return True

    async def _recv(self, response, use_tools=True):
        """takes a response object and extracts the message from it, handling tool calls if needed"""

        try:
            # normal non-streaming mode
            response_main = response.choices[0]
        except Exception as e:
            raise e # raise it so send() can catch it

        reasoning_content = getattr(response_main.message, "reasoning_content", None) or \
                            getattr(response_main.message, "reasoning", None) or ""

        if reasoning_content and core.debug:
            self.manager.log("debug:reasoning", reasoning_content)

        # extract message content
        final_content = response_main.message.content or ""

        # handle tool calls, if any
        tool_calls = None
        if use_tools and core.config.get("model").get("use_tools", False) and response_main.message.tool_calls:
            tool_calls = [tc.model_dump(warnings=False) for tc in response_main.message.tool_calls]

        result = {}

        if final_content:
            result["content"] = final_content
        if reasoning_content:
            result["reasoning_content"] = reasoning_content
        if tool_calls:
            result["tool_calls"] = tool_calls

        # role is always assistant, so we force it if for some reason its not present
        result["role"] = "assistant"

        return result

    async def _recv_stream(self, response, use_tools=True):
        """Takes a response object and extracts the message from it, handling tool calls if needed. Streaming version."""
        final_tool_calls = []
        tool_call_buffer = {}

        token_usage = None
        total_prompt_tokens = 0
        total_completion_tokens = 0
        last_token_time = 0

        if not response:
            return

        response_started = False
        try:
            async for chunk in response:
                if not response_started:
                    # mark it as started so that we can detect if the API has returned a blank message
                    # (for example due to an error the inference server, i.e. llamacpp, failed to send)
                    response_started = True

                if self.cancel_request:
                    if hasattr(response, "close"):
                        # support closing
                        await response.close()
                    raise asyncio.CancelledError("Request cancelled")

                # uncomment if trying to see token stream chunks
                #print(chunk)

                if hasattr(chunk, 'prompt_progress') and chunk.prompt_progress is not None:
                    yield {
                        "type": "prompt_progress",
                        "content": chunk.prompt_progress
                    }

                # Calculate time delta for real-time stats
                current_time = time.time()
                delta_ms = (current_time - last_token_time) * 1000
                last_token_time = current_time

                if chunk.choices:
                    streamed_token = chunk.choices[0].delta

                    content_yield = None

                    # handle content token streaming
                    if streamed_token.content:
                        content_yield = {"type": "content", "content": streamed_token.content}

                    # handle reasoning content streaming
                    reason_part = getattr(streamed_token, "reasoning_content", None) or \
                                getattr(streamed_token, "reasoning", None)

                    if reason_part:
                        content_yield = {"type": "reasoning", "content": reason_part}

                    # add timing data to the yielded token
                    if streamed_token.content or reason_part:
                        # Send timing data: Use native if available, otherwise calculate
                        native_timings = getattr(chunk, 'timings', None)
                        if native_timings:
                            content_yield["timings"] = native_timings

                        else:
                            # Fallback: Calculate tokens/s based on time between chunks
                            if delta_ms > 1: # Only yield if significant time passed
                                content_yield["timings"] = {
                                    "predicted_ms": delta_ms,
                                    "predicted_n": 1
                                }

                    # and finally, yield the content token
                    if content_yield:
                        yield content_yield

                    # extract tool calls, if any
                    if streamed_token.tool_calls and use_tools:
                        for tool_call in streamed_token.tool_calls:
                            index = tool_call.index

                            if index not in tool_call_buffer:
                                tool_call_buffer[index] = tool_call
                                # ensure arguments is always a string
                                if tool_call_buffer[index].function.arguments is None:
                                    tool_call_buffer[index].function.arguments = ""

                                yield {
                                    "type": "tool_call_delta",
                                    "tool_calls": [tool_call_buffer[index].model_dump()]
                                }
                            else:
                                # the documentation for this was awful, so i had to use AI to figure it out
                                # welcome to the reason i was forced to introduce AI slop to the core framework
                                # (dont worry, i removed it by now)
                                # thanks openAI for ruining your documentation of chat completion requests in favor of your stupid Responses API

                                # it seems these properties will only show up in one chunk,
                                # and the rest of the stream won't have them anymore..
                                # so the AI (GLM-5) decided we should set these if they show up
                                # and then just assume it won't happen again
                                # i guess if it does, it just overwrites it..
                                if tool_call.id:
                                    tool_call_buffer[index].id = tool_call.id
                                if tool_call.function.name:
                                    tool_call_buffer[index].function.name = tool_call.function.name

                                # function arguments seem to be the part that actually gets streamed
                                # and which we must accumulate to get the full toolcall
                                if tool_call.function.arguments:
                                    tool_call_buffer[index].function.arguments += tool_call.function.arguments

                                    # the magic sauce that allows streaming toolcall arguments
                                    yield {
                                        "type": "tool_call_delta",
                                        "tool_calls": [tool_call_buffer[index].model_dump()]
                                    }
                                    # we use model_dump() so that it converts the pydantic models to python dicts that can be json serialized

                # if response has usage data, save it so we can use it to show to the user and to trim context
                if hasattr(chunk, 'usage') and chunk.usage is not None:
                    if hasattr(chunk.usage, 'prompt_tokens'):
                        total_prompt_tokens = chunk.usage.prompt_tokens
                    if hasattr(chunk.usage, 'completion_tokens'):
                        total_completion_tokens = chunk.usage.completion_tokens
                    if hasattr(chunk.usage, 'total_tokens'):
                        token_usage = chunk.usage.total_tokens
                    elif total_prompt_tokens > 0 or total_completion_tokens > 0:
                        # Calculate total if not provided
                        token_usage = total_prompt_tokens + total_completion_tokens

                    yield {"type": "token_usage", "content": token_usage, "source": "API"}

                if hasattr(chunk, 'timings'):
                    yield {"type": "timings", "content": chunk.timings}

            if use_tools:
                for index in sorted(tool_call_buffer.keys()):
                    # filter out blank tool calls (rare model glitch)
                    tool_call = tool_call_buffer[index]
                    if not tool_call.function.name:
                        continue

                    final_tool_calls.append(tool_call)

                if final_tool_calls and core.config.get("model").get("use_tools", False):
                    # yield the full toolcall object as a single token to be interpreted by the function that is iterating through _recv_stream()
                    tool_call_dicts = [tc.model_dump(warnings=False) for tc in final_tool_calls]
                    yield {"type": "tool_calls", "tool_calls": tool_call_dicts}

        except Exception as e:
            #self.manager.log_error("error while receiving response from AI", e)
            raise e # Re-raise so send_stream can catch it and yield the error type

        if not response_started:
            # this means the inference server (such as llamacpp) failed to report an error
            # for user friendliness, we just display a message that explains what happened
            raise Exception("The API returned a blank response. This likely means an error happened on the API end, but that API failed to report the error. Check your AI server's logs if you're using local AI, or if not, contact your cloud AI service. Also, try turning off `Use Developer Role` in the api settings, as some models don't support it.")

    async def list_models(self):
        if not self.connected:
            result = await self.attempt_connect()
            return result

        try:
            # get alphabetically sorted model list
            models_model = await self._AI.models.list()

            # commented out but this is going to allow much more detailed model information once i get to implementing it
            # models = models_model.model_dump()

            model_list = [model.id for model in models_model.data]
            model_list.sort()

        except Exception as e:
            self.manager.log_error("error while retrieving model list", e)
            return []

        return model_list
