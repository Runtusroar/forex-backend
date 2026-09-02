import asyncio
from collections.abc import Awaitable, Callable

Loop = Callable[[asyncio.Event], Awaitable[None]]


class BackgroundRuntime:
    def __init__(self, loops: list[Loop], shutdown_timeout: float = 10) -> None:
        self.loops = loops
        self.shutdown_timeout = shutdown_timeout
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.stop_event.clear()
        self.tasks = [asyncio.create_task(loop(self.stop_event)) for loop in self.loops]

    async def stop(self) -> None:
        self.stop_event.set()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=self.shutdown_timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
