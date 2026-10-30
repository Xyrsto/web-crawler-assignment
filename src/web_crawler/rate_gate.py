"""Request pacing: a minimum interval between the starts of requests."""

import asyncio


class RateGate:
    """Enforces a minimum interval between the starts of requests (for example Crawl-delay)."""

    def __init__(self, interval: float = 0.0) -> None:
        self._interval = interval
        self._next_start = 0.0
        self._lock = asyncio.Lock()

    def set_interval(self, interval: float) -> None:
        self._interval = interval

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:  # sleeping under the lock queues the other callers behind us
            loop = asyncio.get_running_loop()
            delay = self._next_start - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_start = loop.time() + self._interval
