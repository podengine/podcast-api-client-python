"""Deadline/retry tests use a virtual monotonic clock and no real retry sleeps."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from podengine import AsyncPodEngine, PodEngine, PodEngineConnectionError, RequestOptions
from podengine._core import _client
from podengine._core._client import AsyncPodEngineCore, EndpointDescriptor, PodEngineCore

GET = EndpointDescriptor("GET", "/test", (), (), "none", False)


class Clock:
    now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(_client.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(_client.time, "sleep", clock.advance)
    monkeypatch.setattr(_client.random, "random", lambda: 0.0)
    return clock


@pytest.mark.parametrize(
    "value", ["", " ", "-1", "NaN", "Infinity", "-Infinity", "1e309", "1e2", "0x10", "garbage", "9" * 400]
)
def test_retry_after_rejects_invalid(value: str) -> None:
    assert _client._retry_after_seconds(httpx.Response(503, headers={"retry-after": value})) is None


def test_retry_after_numeric_and_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_client.time, "time", lambda: 1767225600.0)
    for value, expected in [
        ("0", 0),
        (" 1.5 ", 1.5),
        ("Thu, 01 Jan 2026 00:00:02 GMT", 2),
        ("Wed, 31 Dec 2025 23:59:59 GMT", 0),
    ]:
        assert _client._retry_after_seconds(httpx.Response(503, headers={"retry-after": value})) == expected


@pytest.mark.parametrize("asynchronous", [False, True])
def test_7200_retry_fails_promptly_and_closes_without_reading(clock: Clock, asynchronous: bool) -> None:
    calls = 0
    closed = False

    class Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("retry body must not be read")
            yield b""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise AssertionError("retry body must not be read")
            yield b""

        def close(self) -> None:
            nonlocal closed
            closed = True

        async def aclose(self) -> None:
            self.close()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"retry-after": "7200"}, stream=Stream())

    with pytest.raises(PodEngineConnectionError, match="Retry delay.*cannot fit"):
        if asynchronous:

            async def go() -> None:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await AsyncPodEngineCore(api_key="k", http_client=client).request(GET)

            asyncio.run(go())
        else:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                PodEngineCore(api_key="k", http_client=client).request(GET)
    assert calls == 1
    assert clock.now == 0
    assert closed


@pytest.mark.parametrize("asynchronous", [False, True])
def test_budget_shared_by_attempts_and_retry_sleeps(
    clock: Clock, monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    calls = 0
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        timeouts.append(request.extensions["timeout"]["read"])
        clock.advance(0.4)
        if calls == 1:
            return httpx.Response(503, headers={"retry-after": "0.1"})
        clock.advance(1)
        return httpx.Response(200, json={"data": "late"})

    async def sleep(delay: float) -> None:
        clock.advance(delay)

    monkeypatch.setattr(_client.asyncio, "sleep", sleep)
    with pytest.raises(PodEngineConnectionError, match="Overall request deadline"):
        if asynchronous:

            async def go() -> None:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await AsyncPodEngineCore(api_key="k", total_timeout=1, timeout=10, http_client=client).request(GET)

            asyncio.run(go())
        else:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                PodEngineCore(api_key="k", total_timeout=1, timeout=10, http_client=client).request(GET)
    assert calls == 2
    assert timeouts == pytest.approx([1, 0.499])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("binary", [False, True])
def test_response_reads_consume_budget_and_close(clock: Clock, asynchronous: bool, binary: bool) -> None:
    closed = False

    class Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'{"data":'
            clock.advance(2)
            yield b'"ok"}'

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self:
                yield chunk

        def close(self) -> None:
            nonlocal closed
            closed = True

        async def aclose(self) -> None:
            self.close()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stream())

    descriptor = EndpointDescriptor("GET", "/test", (), (), "none", binary)
    with pytest.raises(PodEngineConnectionError, match="Overall request deadline"):
        if asynchronous:

            async def go() -> None:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await AsyncPodEngineCore(api_key="k", total_timeout=1, http_client=client).request(descriptor)

            asyncio.run(go())
        else:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                PodEngineCore(api_key="k", total_timeout=1, http_client=client).request(descriptor)
    assert closed


@pytest.mark.parametrize("asynchronous", [False, True])
def test_public_client_override_and_minimum_retry_jitter(
    clock: Clock, monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(503, headers={"retry-after": "2"})
            if calls == 1
            else httpx.Response(200, json={"data": {"options": {}, "chart": None}})
        )

    def sync_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)

    async def async_sleep(delay: float) -> None:
        sync_sleep(delay)

    monkeypatch.setattr(_client.time, "sleep", sync_sleep)
    monkeypatch.setattr(_client.asyncio, "sleep", async_sleep)
    options = RequestOptions(total_timeout=3)
    if asynchronous:

        async def go() -> Any:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                pe = AsyncPodEngine("k", total_timeout=1, http_client=client)
                return await pe.charts.get_latest_chart(
                    chart_type="apple", country="us", category="top podcasts", request_options=options
                )

        asyncio.run(go())
    else:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            pe = PodEngine("k", total_timeout=1, http_client=client)
            pe.charts.get_latest_chart(
                chart_type="apple", country="us", category="top podcasts", request_options=options
            )
    assert calls == 2
    assert sleeps == [2.001]


@pytest.mark.parametrize("phase", ["fetch", "sleep", "body"])
def test_async_cancellation_propagates(phase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        started = asyncio.Event()
        closed = False

        async def block(*args: Any, **kwargs: Any) -> None:
            started.set()
            await asyncio.Event().wait()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                await block()
                yield b""

            async def aclose(self) -> None:
                nonlocal closed
                closed = True

        async def handler(request: httpx.Request) -> httpx.Response:
            if phase == "fetch":
                await block()
            if phase == "sleep":
                return httpx.Response(503, headers={"retry-after": "1"})
            return httpx.Response(200, stream=Stream())

        if phase == "sleep":
            monkeypatch.setattr(_client.asyncio, "sleep", block)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(AsyncPodEngineCore(api_key="k", http_client=client).request(GET))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        if phase == "body":
            assert closed

    asyncio.run(go())


def test_async_overall_timer_interrupts_stalled_read(clock: Clock) -> None:
    async def go() -> None:
        started = asyncio.Event()
        closed = False

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self) -> None:
                nonlocal closed
                closed = True

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        ) as client:
            task = asyncio.create_task(
                AsyncPodEngineCore(api_key="k", total_timeout=1, http_client=client).request(GET)
            )
            await started.wait()
            clock.advance(2)
            with pytest.raises(PodEngineConnectionError, match="Overall request deadline"):
                await task
        assert closed

    asyncio.run(go())


def test_request_options_positional_compatibility() -> None:
    options = RequestOptions(10, 0, {"x-test": "1"})
    assert options.timeout == 10 and options.max_retries == 0 and options.headers == {"x-test": "1"}
    assert options.total_timeout is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_disable_deadline_preserves_no_io_timeout(clock: Clock, asynchronous: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert all(value is None for value in request.extensions["timeout"].values())
        clock.advance(120)
        return httpx.Response(200, json={"data": "ok"})

    options = RequestOptions(total_timeout=0)
    if asynchronous:

        async def go() -> Any:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await AsyncPodEngineCore(api_key="k", timeout=None, http_client=client).request(
                    GET, options=options
                )

        assert asyncio.run(go()) == "ok"
    else:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            assert PodEngineCore(api_key="k", timeout=None, http_client=client).request(GET, options=options) == "ok"


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_total_timeout(value: float) -> None:
    with pytest.raises(ValueError, match="total_timeout"):
        PodEngineCore(api_key="k", total_timeout=value)
