import asyncio

from counter import AsyncCounter


def test_concurrent_increments_are_not_lost():
    async def scenario():
        counter = AsyncCounter()
        await asyncio.gather(*(counter.increment() for _ in range(100)))
        return counter.value

    assert asyncio.run(scenario()) == 100
