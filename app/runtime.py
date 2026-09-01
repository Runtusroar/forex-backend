import asyncio
from collections.abc import Awaitable, Callable

Loop = Callable[[asyncio.Event], Awaitable[None]]


class BackgroundRuntime:
    def __init__(self, loops: list[Loop]) -> None:
        self.loops = loops
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.stop_event.clear()
        self.tasks = [asyncio.create_task(loop(self.stop_event)) for loop in self.loops]

    async def stop(self) -> None:
        self.stop_event.set()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
