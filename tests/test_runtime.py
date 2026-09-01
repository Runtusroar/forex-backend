import asyncio

from app.runtime import BackgroundRuntime


class Loop:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def run(self, stop: asyncio.Event) -> None:
        self.started = True
        await stop.wait()
        self.stopped = True


async def test_runtime_starts_and_stops_all_background_loops() -> None:
    first = Loop()
    second = Loop()
    runtime = BackgroundRuntime([first.run, second.run])

    await runtime.start()
    await asyncio.sleep(0)
    await runtime.stop()

    assert first.started and second.started
    assert first.stopped and second.stopped
