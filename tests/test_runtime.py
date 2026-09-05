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


async def test_runtime_cancels_a_worker_that_exceeds_shutdown_deadline() -> None:
    cancelled = asyncio.Event()

    async def stuck(_stop: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime = BackgroundRuntime([stuck], shutdown_timeout=0.01)
    await runtime.start()
    await asyncio.sleep(0)

    await runtime.stop()

    assert cancelled.is_set()


async def test_runtime_restarts_worker_after_unexpected_exit():
    calls = 0
    restarted = asyncio.Event()

    async def crashing(stop):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("worker failed")
        restarted.set()
        await stop.wait()

    runtime = BackgroundRuntime([crashing], restart_backoff=0.001)
    await runtime.start()
    await asyncio.wait_for(restarted.wait(), 1)
    await runtime.stop()
    assert calls == 2


async def test_runtime_never_restarts_cancelled_worker():
    calls = 0

    async def cancelled(_stop):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    runtime = BackgroundRuntime([cancelled], restart_backoff=0.001)
    await runtime.start()
    await asyncio.sleep(0.01)
    await runtime.stop()
    assert calls == 1


async def test_runtime_restarts_returned_worker_but_stop_interrupts_backoff():
    calls = 0
    first_returned = asyncio.Event()

    async def returns(_stop):
        nonlocal calls
        calls += 1
        first_returned.set()

    runtime = BackgroundRuntime([returns], restart_backoff=10)
    await runtime.start()
    await first_returned.wait()
    await asyncio.wait_for(runtime.stop(), timeout=0.1)
    assert calls == 1
