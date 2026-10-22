import core
import os
import asyncio
import copy

class Messages:
    def __init__(self, channel, chat):
        self.channel = channel
        self.chat = chat

        chat_id = self.chat.get("id")
        if not isinstance(chat_id, str):
            raise Exception("Could not load chat messages: Chat ID must be a string")
        # which chat this history belongs to, fixed at load time (the chat's
        # index in the chat list can shift when other chats are deleted)
        self.chat_id = chat_id

        self.path = os.path.join(self.chat.path, "history", self.chat.get("id"))
        # compact_json: history is rewritten on every save, so skip indent + ascii escaping
        self.data = core.storage.StorageList(self.path, "json", compact_json=True)

        # saves are debounced: rapid adds (tool call chains, streams) coalesce
        # into a single disk write instead of one full rewrite per message
        self._save_task = None
        # True while there are changes that haven't been written yet
        self._dirty = False
        self.SAVE_DEBOUNCE_SECONDS = 0.15
        # serializes the actual disk write: history can hold several MB of
        # base64 attachments, so the dump+write+fsync runs in a worker thread
        # (see save()); the lock keeps concurrent saves of this file ordered
        self._save_lock = asyncio.Lock()

    def _schedule_save(self):
        self._dirty = True
        if self._save_task and not self._save_task.done():
            return
        self._save_task = asyncio.create_task(self._debounced_save())

    async def flush(self):
        """write pending (debounced) changes now and wait for any write in flight,
        so the file on disk is complete. used before switching away from a chat:
        reopening it reads the file, and a save still pending at that moment would
        make it load without its latest messages (and the next save would then
        overwrite them). no-op when nothing is pending"""
        if self._dirty:
            await self.save()
        async with self._save_lock:
            pass

    async def discard(self):
        """drop pending (debounced) changes and wait for any write in flight. used
        before deleting this chat's file, so a late save can't write it back"""
        self._dirty = False
        task, self._save_task = self._save_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._save_lock:
            pass

    async def _debounced_save(self):
        await asyncio.sleep(self.SAVE_DEBOUNCE_SECONDS)
        self._save_task = None
        await self.save()

    async def save(self):
        """flush any pending debounced save immediately, then write to disk"""

        task, self._save_task = self._save_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self.chat.update_timestamp()
        # the history file can hold several MB of base64 attachments; run the
        # synchronous json dump + atomic write + fsync in a worker thread so
        # a big chat never blocks the event loop mid agentic-loop
        async with self._save_lock:
            # snapshot on the loop: appends and in-place stream edits keep
            # happening while the thread dumps, so the thread must not walk the
            # live list. deepcopy shares the (immutable) strings, so big base64
            # attachments cost nothing extra - only the containers are copied
            self._dirty = False
            snapshot = copy.deepcopy(list(self.data))
            return await asyncio.to_thread(self.data.save, snapshot)

    async def get(self, index = None):
        """get message history of current chat"""
        # allow targeting a specific index
        if index is not None:
            if index >= len(self.data):
                raise Exception("Invalid message index")

            return self.data[index]

        # if no index is specified, just return the entire message history
        return self.data

    async def add(self, message: dict, cmd=False, ghost = False):
        """add message to current chat"""
        # make a copy so we don't modify the original reference
        new_message = message.copy()
        if "_metadata" not in new_message.keys():
            new_message["_metadata"] = {}

        # signal messages (eg. meeting boundaries) must never hijack the chat title
        is_signal = bool(message.get("_metadata", {}).get("signal"))
        untitled = not self.chat.get("title") or self.chat.get("title") == "New chat"
        if untitled and message.get("role") == "user" and not cmd and not is_signal:
            # auto-set title (if the message was not a command)
            msg_content = self.channel._extract_content(new_message)
            if isinstance(msg_content, str):
                await self.chat.set("title", msg_content[:100]+".." if len(msg_content) > 100 else msg_content)
            else:
                # this happens when the user uploads a media file. don't set that as a title, lol
                pass

        # if marked as a ghost message, set the flag. gets handled in self.trim()
        # ghost messages are invisible to the AI
        if ghost:
            new_message["_metadata"]["ghost"] = True

        if cmd:
            # if the message is a command (or command response), mark it as such
            new_message["_metadata"]["is_cmd"] = True

        # inject any special messages coming from on_message_inject() in modules, such as timestamps
        injections = []
        if message.get("role") == "user":
            for module_name, module in self.channel.manager.modules.items():
                if hasattr(module, 'on_message_inject'):
                    try:
                        injection = await module.on_message_inject()
                        if injection:
                            injections.append(injection)
                    except Exception as e:
                        self.channel.log("module error", f"{module.name}: in on_message_inject(): {core.detail_error(e)}")

            if injections:
                new_message["_metadata"]["injection"] = "\n\n".join(injections)

        self.data.append(new_message)
        self._schedule_save()
        return True

    async def edit(self, index: int, message):
        """edit message by its index"""
        if index >= len(self.data):
            return False

        self.data[index] = message
        await self.save()

    async def delete(self, index: int):
        """delete message from current chat"""
        self.data.pop(index)
        index = len(self.data) - 1
        await self.save()

        return index

    async def delete_from(self, index: int):
        """
        Deletes the message at the given index and all messages after it
        """
        if index >= len(self.data):
            raise Exception("Invalid message index")

        # return all messages up to (but not including) the target message
        new_messages = self.data[:index]

        self.data.load(new_messages)
        await self.save()
        return True

    async def clear(self):
        self.data.clear()
        # persist the wipe: without a save here the old history file would
        # resurrect the messages on the next restart
        await self.save()
        return True

    async def get_last_message_with_role(self, role: str, cutoff_index: int = None):
        """gets the latest message with the specified role"""

        # if we have a "cutoff index",
        # it means we have to search backwards
        # from that index
        # which is very useful for, say,
        # regenerating a message
        # because we can target the last user message
        # before the cutoff index

        if not self.data:
            return -1

        if len(self.data) == 1:
            # just return 0 if there is only one message... but only if the role matches the request
            if self.data[0].get("role") == role:
                return 0
            return -1

        if cutoff_index is not None:
            # clamp it
            start_index = min(cutoff_index, len(self.data) - 1)
        else:
            start_index = len(self.data) - 1

        for index in range(start_index, -1, -1):
            if self.data[index].get("role") == role:
                return index

        return -1