import asyncio
import itertools
import time

import pytest

from web_crawler.rate_gate import RateGate


async def test_spaces_out_request_starts() -> None:
    gate = RateGate(0.05)
    started = time.monotonic()
    for _ in range(3):
        await gate.wait()
    assert time.monotonic() - started >= 0.09


async def test_spaces_out_callers_that_arrive_together() -> None:
    gate = RateGate(0.05)
    starts: list[float] = []

    async def request() -> None:
        await gate.wait()
        starts.append(time.monotonic())

    await asyncio.gather(*(request() for _ in range(4)))

    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert len(gaps) == 3
    assert all(gap >= 0.04 for gap in gaps)


async def test_without_an_interval_never_waits() -> None:
    gate = RateGate()
    started = time.monotonic()
    for _ in range(50):
        await gate.wait()
    assert time.monotonic() - started < 0.05


@pytest.mark.parametrize("interval", [0.0, -1.0])
async def test_ignores_non_positive_intervals(interval: float) -> None:
    gate = RateGate()
    gate.set_interval(interval)
    started = time.monotonic()
    await gate.wait()
    await gate.wait()
    assert time.monotonic() - started < 0.05


async def test_an_interval_set_later_applies_to_later_waits() -> None:
    gate = RateGate()
    await gate.wait()  # no interval yet, so this is free

    gate.set_interval(0.05)
    started = time.monotonic()
    await gate.wait()
    await gate.wait()

    assert time.monotonic() - started >= 0.04
