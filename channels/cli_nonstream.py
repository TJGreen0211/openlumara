import core

class CliNonstream(core.channel.Channel):
    async def run(self):
        while True:
            user_input = input("> ")
            response = await self.send(user_input)
            print(response.get("token"), flush=True)

    async def on_push(self, message):
        print("\n"+message.get("content"), flush=True)
