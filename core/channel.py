import core
import core.commands
import os
import sys
import time
import json
import asyncio

class Channel:
    """Base class for channels"""

    settings = {
        # base settings here lol
    }

    def __init__(self, manager):
        self.manager = manager
        self.name = core.modules.get_name(self) # shorthand alias
        self.commands = core.commands.Commands(self)
        self._last_cmd_was_temporary = False
        self.context = core.context.Context(self) # each channel has its own context window

        self.tc_manager = core.toolcalls.ToolcallManager(self)

        # load channel config
        self.config = core.config.ConfigManager(core.config.config, ["channels", "settings", self.name])

        self._shutting_down = False

        # start the "push queue" which handles messages that are pushed to channels without
        # the user first sending a message. this is what powers announcements and the like
        self.push_queue = asyncio.Queue()
        self._queue_task = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # merge the base class's settings with the subclass settings.
        # this way, we can define settings ALL channels should have
        for b in cls.__mro__[1:]:
            if hasattr(b, "settings"):
                cls.settings = b.settings | cls.settings
                break

    async def _shutdown(self):
        """internal shutdown function. gets called by the manager before on_shutdown()"""

        self._shutting_down = True
        if self._queue_task:
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass

    async def _set_as_active_channel(self):
        self.manager.channel = self

        # give all modules a way to access this channel
        for module_name, module in self.manager.modules.items():
            module.channel = self

    def _get_disconnection_message(self):
        status = self.manager.get_api_status()
        error = status.get("error", "Unknown error")

        message_parts = ["Not connected to API."]

        if error:
            message_parts.append(f"Error: {error}")

        if not status.get("url_configured"):
            message_parts.append("Please configure your API URL in config/config.yml")
        elif not status.get("key_configured"):
            message_parts.append("Please configure your API key in config/config.yml")
        else:
            message_parts.append("Use /connect to retry connection, or check your settings.")

        return "\n".join(message_parts)

    def _extract_content(self, message_dict):
        """helper method that makes sure we always get the text content as a string from the messages array, even if it's multimodal"""
        content = message_dict.get("content")

        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            # it's multimodal
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text")

        # fallback
        return ""

    def format_message(self, message: dict):
        formatted = ""

        role = message.get("role")

        reasoning_content = None

        if role in ("user", "assistant"):
            if self.config.get("show_reasoning"):
                reasoning_content = message.get("reasoning_content")
                if reasoning_content:
                    formatted += f"**Reasoning:**\n{reasoning_content}\n\n"

            content = message.get("content")
            if content:
                if reasoning_content:
                    formatted += "**Conclusion**:\n"

                formatted += f"{content}\n\n"

        if role == "assistant":
            if message.get("tool_calls"):
                for tool_call in message.get("tool_calls"):
                    formatted += self.tc_manager.display_call(tool_call)+"\n"

                formatted += "\n\n"

        if role == "tool":
            formatted = "processing results.."

        message["content"] = formatted.strip()

        return message

    async def _start_push_queue(self):
        if not hasattr(self, "on_push"):
            return
        core.log(self.name, "starting message push queue consumer")
        self._queue_task = asyncio.create_task(self._push_consumer())

    async def _push_consumer(self):
        """Consumes messages from the queue and triggers on_push sequentially"""
        while not getattr(self, "_shutting_down", False):
            try:
                message = await self.push_queue.get()
                await self.on_push(self.format_message(message))
                self.push_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Always log full traceback for easier debugging
                import traceback
                traceback.print_exc()
                core.log(self.name, f"error in message consumer: {str(e)}")
                await asyncio.sleep(0.5)

    # async def _poll_loop(self):
    #     """constantly polls the chat history to see if anything new arrived, and triggers on_message for every new message"""
    #     if not hasattr(self, "on_message"):
    #         return False
    #
    #     core.log(self.name, "started message polling loop")
    #
    #     while not getattr(self, "_shutting_down", False):
    #         try:
    #             # check for new messages
    #             new_messages = await self.context.chat.get_new()
    #
    #             if new_messages:
    #                 for message in new_messages:
    #                     # trigger the event
    #                     await self.on_message(self.format_message(message))
    #
    #             await asyncio.sleep(0.1)
    #
    #         except Exception as e:
    #             core.log(self.name, f"error in poll loop: {str(e)}")
    #             # if we hit an error, back off for a second so we don't spam the logs
    #             await asyncio.sleep(1)

    async def send(self, message: dict):
        """sends a message to the AI from within the current channel"""

        # as soon as user sends a message in this channel, set current channel (tracked in the manager) to this one
        await self._set_as_active_channel()

        # process any /commands
        if isinstance(message.get("content"), str):
            cmd_response = None
            is_cmd = message.get("content", "").strip().lower().startswith(
                core.config.get("core", "cmd_prefix").strip().lower()
            )

            if is_cmd and message.get("role", "user") == "user":
                try:
                    cmd_response = await self.commands.process_input(message)
                except Exception as e:
                    # Always log full traceback for easier debugging
                    import traceback
                    traceback.print_exc()
                    core.log_error("error while executing command", e)

                if cmd_response:
                    return {"role": "assistant", "content": cmd_response}
                else:
                    return {"role": "assistant", "content": "BLANK"}

        # if not a command, send the message to the AI and return it's response

        # attempt auto-reconnect once
        if not self.manager.API.connected:
            reconnected = await self.manager.API.connect()
            if not reconnected:
                return {"role": "assistant", "content": self._get_disconnection_message()}

        # add sent message to context
        add_success = await self.context.chat.add(message)

        if not add_success:
            return None

        # run module event hooks
        for module_name, module in self.manager.modules.items():
            if hasattr(module, "on_user_message"):
                try:
                    if asyncio.iscoroutinefunction(module.on_user_message):
                        await module.on_user_message(message.get("content", ""))
                    else:
                        module.on_user_message(message.get("content", ""))
                except Exception as e:
                    # Always log full traceback for easier debugging
                    import traceback
                    traceback.print_exc()
                    core.log(module.name, f"could not run user message hook: {e}")

        # then get the full context window
        context = await self.context.get(system_prompt=True, end_prompt=True)

        # and then request the AI response and add it to context
        response = await self.manager.API.send(context)

        # handle any errors
        if isinstance(response, dict) and "error" in response:
            await self.context.chat.pop()  # remove the user message we just added
            error_msg = response.get("message", "Unknown error occurred")
            return {"role": "assistant", "content": f"API Error: {error_msg}\n\nUse /connect to retry."}

        # make a copy of the response message and edit it
        assistant_message = dict(response)
        assistant_message["role"] = "assistant"

        tool_calls = assistant_message.get("tool_calls")

        # convert any toolcalls to a dict so that JSON serialization doesnt die
        if tool_calls:
            toolcalls_converted = []

            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    tool_call = tool_call.model_dump(warnings=False)
                toolcalls_converted.append(tool_call)

            assistant_message["tool_calls"] = toolcalls_converted

        if tool_calls:
            # process() does all the toolcalling, but it also returns the raw toolcall stream for our own use
            async for sub_token in self.tc_manager.process(
                assistant_message,
                push=True
            ):
                # push handles all the output
                pass

        # add to context
        if not tool_calls:
            await self.context.chat.add(assistant_message)

        # run module event hooks
        for module_name, module in self.manager.modules.items():
            if hasattr(module, "on_assistant_message"):
                try:
                    if asyncio.iscoroutinefunction(module.on_assistant_message):
                        await module.on_assistant_message(assistant_message.get("content", ""))
                    else:
                        module.on_assistant_message(assistant_message.get("content", ""))
                except Exception as e:
                    # Always log full traceback for easier debugging
                    import traceback
                    traceback.print_exc()
                    core.log(module.name, f"could not run assistant message hook: {e}")

        if tool_calls:
            return None

        return self.format_message(assistant_message)

    async def send_stream(self, message: dict):
        """sends a message to the AI from within the current channel, streaming version"""

        # as soon as user sends a message in this channel, set current channel (tracked in the manager) to this one
        await self._set_as_active_channel()

        user_message = message #alias for readability

        # process any /commands
        if isinstance(message.get("content"), str):
            cmd_response = None
            is_cmd = message.get("content", "").strip().lower().startswith(
                core.config.get("core", "cmd_prefix").strip().lower()
            )

            if is_cmd and message.get("role", "user") == "user":
                try:
                    cmd_response = await self.commands.process_input(user_message)
                except Exception as e:
                    core.log_error("error while executing command", e)

                if cmd_response:
                    # insert and return the command response without sending it to the AI
                    for word in cmd_response:
                        yield {"type": "content", "content": word}
                    return

        # attempt auto-reconnect once
        if not self.manager.API.connected:
            reconnected = await self.manager.API.connect()
            if not reconnected:
                yield {"type": "content", "content": self._get_disconnection_message()}
                return

        # add user's message to context
        add_success = await self.context.chat.add(user_message)
        if not add_success:
            return

        # estimate tokens used for user message
        user_message_token_estimation = 0
        if self.context.chat.using_api_token_data:
            # if using API token count
            user_msg_tokens = await self.context.chat.count_tokens([user_message])
            user_message_token_estimation = await self.context.chat.get_token_usage()+user_msg_tokens

            # add to existing API token count
            await self.context.chat.set_token_usage(user_message_token_estimation)
        else:
            # just fully estimate
            user_message_token_estimation = await self.context.chat.count_tokens()

        # yield so it updates throughout all channels that display token count
        yield {"type": "token_usage", "content": user_message_token_estimation, "source": "estimation"}

        # run module event hooks
        for module_name, module in self.manager.modules.items():
            if hasattr(module, "on_user_message"):
                try:
                    if asyncio.iscoroutinefunction(module.on_user_message):
                        await module.on_user_message(message.get("content", ""))
                    else:
                        module.on_user_message(message.get("content", ""))
                except Exception as e:
                    # Always log full traceback for easier debugging
                    import traceback
                    traceback.print_exc()
                    core.log(module.name, f"could not run user message hook: {e}")

        # get the new context window with the added message
        context = await self.context.get(system_prompt=True, end_prompt=True)

        final_content = []
        final_reasoning = []
        tc_response = None
        tool_calls_occurred = False
        fetched_token_usage = False

        # and stream the response to the caller of this method
        async for token in self.manager.API.send_stream(context):
            token_type = token.get("type")

            # handle any errors
            if token_type == "error":
                error_data = token.get("content", {})
                error_msg = error_data.get("message", "Unknown error")
                yield {"type": "content", "content": f"API Error: {error_msg}"}
                return

            if token_type == "content":
                # this is a normal piece of streamed text
                final_content.append(token.get("content"))
                yield token
            elif token_type == "reasoning":
                final_reasoning.append(token.get("content"))
                yield token
            elif token_type == "tool_call_delta":
                # yay toolcall arg streaming!
                yield token
            elif token_type == "tool_calls":
                yield token
                tool_calls_occurred = True

                toolcall_request = await self.tc_manager._build_recursive_request(token, final_content, final_reasoning)

                # we add the accumulated content tokens so far to the assistant_content argument
                async for sub_token in self.tc_manager.process(toolcall_request):
                    yield sub_token
                # tc_manager.process() will loop until the AI no longer deems tool calls necessary
            elif token_type == "tool":
                # this is a toolcall response
                yield token
            elif token_type == "token_usage":
                # this is the final token usage count, usually emitted at the end of the stream
                token_usage = token.get("content")
                if isinstance(token_usage, int):
                    # set the flag so that token counting is always using API data
                    if not self.context.chat.using_api_token_data:
                        self.context.chat.using_api_token_data = True

                    # cache this so chat.get_token_usage() returns this value
                    await self.context.chat.set_token_usage(token_usage)

                    fetched_token_usage = True
                yield token

        if not fetched_token_usage:
            # yield an estimated token usage if the API didn't provide one
            yield {"type": "token_usage", "content": await self.context.chat.count_tokens(), "source": "estimation"}

        if not tool_calls_occurred and final_content: # don't add an extra message at the end of a toolcalling chain
            # add the assistant's response to context
            assistant_message = {
                "role": "assistant",
                "content": "".join(final_content)
            }

            if final_reasoning:
                assistant_message["reasoning_content"] = "".join(final_reasoning)

            await self.context.chat.add(assistant_message)

            # run module event hooks
            for module_name, module in self.manager.modules.items():
                if hasattr(module, "on_assistant_message"):
                    try:
                        if asyncio.iscoroutinefunction(module.on_assistant_message):
                            await module.on_assistant_message(assistant_message.get("content", ""))
                        else:
                            module.on_assistant_message(assistant_message.get("content", ""))
                    except Exception as e:
                        # Always log full traceback for easier debugging
                        import traceback
                        traceback.print_exc()
                        core.log(module.name, f"could not run assistant message hook: {e}")

    async def on_push(self, message: dict):
        raise NotImplementedError

    async def push(self, message):
        """
        push a message to the push queue, which will instantly display it in all channels
        without adding to context, making it invisible to the AI
        """

        if not hasattr(self, "push_queue"):
            return False

        # message can be either a str or a dict.
        # if dict, just use it as-is
        # otherwise, turn it into an openAI message dict
        if isinstance(message, dict):
            await self.push_queue.put(message)
            # if add_to_context:
            #      self.context.chat.add(message)
        else:
            await self.push_queue.put({"role": "assistant", "content": str(message)})
            # if add_to_context:
            #     await self.context.chat.add({"role": "assistant", "content": str(message)})

    async def announce(self, message: str, type=None):
        """called externally to announce things in this channel, such as a reminder sent by the AI"""
        if not type:
            type = "info"

        # insert announced message into context
        await self.context.chat.add({"role": "assistant", "content": f"[System {type}]: {message}"})

        # and push it
        await self.push(message)

    async def announce_all(self, message: str, type=None):
        """announces a message across all channels. useful for very important notifications!"""
        if not type:
            type = "info"

        should_insert = True
        for channel_name, channel in self.manager.channels.items():
            await channel.announce(message, type, insert_message=should_insert)

            if should_insert:
                # insert into context only once
                should_insert = False
        return

    async def ask(self, message: str):
        """sends a message in the channel and then intercepts communication for one message so that user can be asked for input without that input being sent to the LLM. useful for menus."""
        raise NotImplementedError
