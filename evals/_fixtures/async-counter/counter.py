import asyncio


class AsyncCounter:
    def __init__(self) -> None:
        self.value = 0

    async def increment(self) -> None:
        current = self.value
        await asyncio.sleep(0)
        self.value = current + 1
