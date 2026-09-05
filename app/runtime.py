import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Loop = Callable[[asyncio.Event], Awaitable[None]]


class BackgroundRuntime:
    def __init__(
        self,
        loops: list[Loop],
        shutdown_timeout: float = 10,
        restart_backoff: float = 1,
    ) -> None:
        self.loops = loops
        self.restart_backoff = restart_backoff
        self.shutdown_timeout = shutdown_timeout
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.stop_event.clear()
        self.tasks = [asyncio.create_task(self._supervise(loop)) for loop in self.loops]

    async def _supervise(self, loop: Loop) -> None:
        delay = self.restart_backoff
        while not self.stop_event.is_set():
            try:
                await loop(self.stop_event)
                if not self.stop_event.is_set():
                    logger.error("Background worker %s returned unexpectedly", loop)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background worker %s failed; restarting", loop)
            if self.stop_event.is_set():
                return
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except TimeoutError:
                delay = min(delay * 2, 60)

    async def stop(self) -> None:
        self.stop_event.set()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=self.shutdown_timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
