import core

class CliLite(core.channel.Channel):
    """A super lightweight version of the CLI channel that uses basic python input and doesn't use streaming"""

    async def run(self):
        while True:
            user_input = input("> ")
            response = await self.send(user_input)
            print(response.get("token"), flush=True)

    async def on_push(self, message):
        print("\n"+message.get("content"), flush=True)
