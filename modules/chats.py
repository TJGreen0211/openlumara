import core
import datetime

class Chats(core.module.Module):
    """Lets you or the AI manage your chats"""

    settings = {
        "insert_system_prompt": {
            "description": "Make the AI aware of what categories exist for your chats to be sorted into. Highly recommended!",
            "default": True
        }
    }

    async def on_ready(self):
        if not self.config.get("insert_system_prompt"):
            self.disabled_tools.append("get_categories")

    async def on_system_prompt(self):
        if not self.config.get("insert_system_prompt"):
            return None

        cats = await self._get_categories()
        return f"Available categories to categorise chat into: {', '.join(cats)}" if len(cats) > 1 else None

    async def _get_categories(self):
        cats = [c for c in self.channel.context.chat.get_categories() if len(c.split(":")) == 1 and c]
        return cats

    async def get_categories(self):
        cats = await self._get_categories()
        if not cats:
            return self.result("There are no categories yet. Create one!")

        return self.result(cats)

    async def organize(self, new_name: str, category: str, tags: list = None):
        """Lets you rename, categorize, and tag the current chat. If the chat fits within an existing category (defined in your system prompt), use that one. If a fitting category does not exist, create a new one."""
        if not new_name:
            return self.result("name must not be blank", False)

        if tags is None:
            tags = []

        await self.channel.context.chat.set("title", new_name)
        await self.channel.context.chat.set("category", category)
        await self.channel.context.chat.set("tags", tags)
        return self.result(f"chat organised!")

    async def _search(self, query: str, max_results: int = 20):
        return await self.channel.context.chat.search(query, max_results)

    async def search(self, query: str):
        """Searches within all previous chats the user ever had with you. Very useful for recalling information from the past! Use only if user explicitly requests it, or if you can't find a past event the user is referring to within your current context!"""
        found = await self._search(query)
        if not found:
            return self.result("no results found")
        return self.result(found)
