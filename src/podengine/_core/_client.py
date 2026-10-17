"""Transport core for the Pod Engine SDK (sync + async).

The generated resource methods are thin wrappers that hand a static
:class:`EndpointDescriptor` plus the caller's params to ``request``. Every HTTP concern —
auth, URL building, query/body serialization, retries, error normalization and envelope
unwrapping — lives here. The generated layer above turns the unwrapped payload into typed
pydantic models. This module has no internal/monorepo imports so the package publishes and
mirrors standalone.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from .errors import PodEngineAPIError, PodEngineConnectionError
from .transform import build_query_params, extract_error_message, to_jsonable

DEFAULT_BASE_URL = "https://api.podengine.ai"
DEFAULT_SOURCE = "api"
DEFAULT_MAX_RETRIES = 2
DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class EndpointDescriptor:
    """Static metadata the generator emits for each endpoint."""

    method: str
    #: Path template with ``{param}`` placeholders, e.g. ``/api/v1/episodes/{episodeId}/details``.
    path: str
    #: Names of params that fill ``{...}`` placeholders in the path (wire names).
    path_params: tuple[str, ...]
    #: Names of params serialized into the query string (wire names).
    query_params: tuple[str, ...]
    #: How the JSON body is assembled: ``"none"`` | ``"merge"`` (object body) | ``"field"`` (array/primitive body).
    body: str
    #: Whether the success response is a binary download (returned as ``bytes``).
    binary: bool


@dataclass
class RequestOptions:
    """Per-call overrides, merged over the client defaults for a single request."""

    timeout: float | None = None
    max_retries: int | None = None
    headers: dict[str, str] | None = None
    #: Overall budget in seconds; None inherits the client, 0 disables the deadline.
    total_timeout: float | None = None


@dataclass
class _Prepared:
    method: str
    url: str
    params: list[tuple[str, str]]
    json_body: Any
    has_body: bool
    headers: dict[str, str]
    binary: bool


def _is_retriable_status(status: int) -> bool:
    return status == 429 or status == 408 or status >= 500


def _backoff_seconds(attempt: int) -> float:
    # Exponential backoff with jitter, capped at 8s (mirrors the TS SDK).
    base = min(8.0, 0.25 * (2 ** (attempt - 1)))
    return base + base * 0.25 * random.random()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if not header:
        return None
    value = header.strip()
    if re.fullmatch(r"[+]?(?:\d+\.?\d*|\.\d+)", value):
        seconds = float(value)
        return seconds if math.isfinite(seconds) else None
    if not re.match(r"^[A-Za-z]{3},?\s", value):
        return None
    try:
        when = parsedate_to_datetime(value)
        seconds = max(0.0, when.timestamp() - time.time())
        return seconds if math.isfinite(seconds) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_total_timeout(value: float | None) -> None:
    if value is not None and (not math.isfinite(value) or value < 0):
        raise ValueError("total_timeout must be a finite nonnegative number or None.")


class _DeadlineStream(httpx.SyncByteStream):
    """Check the remaining budget between raw reads without background threads."""

    def __init__(self, stream: httpx.SyncByteStream, check: Any) -> None:
        self._stream = stream
        self._check = check

    def __iter__(self):
        iterator = iter(self._stream)
        while True:
            self._check()
            try:
                chunk = next(iterator)
            except StopIteration:
                self._check()
                return
            self._check()
            yield chunk

    def close(self) -> None:
        self._stream.close()


class _BaseCore:
    """Configuration + the pure request/response plumbing shared by both transports."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        source: str = DEFAULT_SOURCE,
        headers: dict[str, str] | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float | None = DEFAULT_TIMEOUT,
        total_timeout: float | None = 60.0,
    ) -> None:
        if not api_key:
            raise PodEngineAPIError(
                message="A Pod Engine `api_key` is required. Get one at https://www.podengine.ai/get-started.",
                status=0,
                method="CONFIG",
                url="",
            )
        self._api_key = api_key
        resolved_base = base_url or os.environ.get("PODENGINE_API_URL") or DEFAULT_BASE_URL
        self._base_url = resolved_base.rstrip("/")
        self._source = source
        self._headers = dict(headers or {})
        self._max_retries = max_retries
        self._timeout = timeout
        _validate_total_timeout(total_timeout)
        self._total_timeout = total_timeout

    def _deadline(self, options: RequestOptions | None) -> float:
        total = options.total_timeout if options and options.total_timeout is not None else self._total_timeout
        _validate_total_timeout(total)
        return time.monotonic() + total if total else math.inf

    def _remaining(self, deadline: float, prepared: _Prepared) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._deadline_error(prepared)
        return remaining

    def _deadline_error(self, prepared: _Prepared) -> PodEngineConnectionError:
        return PodEngineConnectionError(
            message=f"Overall request deadline exceeded for {prepared.url}.",
            method=prepared.method,
            url=prepared.url,
        )

    def _retry_delay(
        self, response: httpx.Response | None, attempt: int, deadline: float, prepared: _Prepared
    ) -> float:
        retry_after = _retry_after_seconds(response) if response is not None else None
        delay = _backoff_seconds(attempt) if retry_after is None else retry_after + 0.001 + random.random() * 0.25
        if delay >= self._remaining(deadline, prepared):
            raise PodEngineConnectionError(
                message=(
                    f"Retry delay of {delay}s cannot fit within the remaining "
                    f"overall request deadline for {prepared.url}."
                ),
                method=prepared.method,
                url=prepared.url,
            )
        return delay

    def _prepare(
        self,
        descriptor: EndpointDescriptor,
        params: dict[str, Any] | None,
        options: RequestOptions | None,
    ) -> _Prepared:
        all_params = params or {}
        path_param_set = set(descriptor.path_params)
        query_param_set = set(descriptor.query_params)

        path = descriptor.path
        for name in descriptor.path_params:
            value = all_params.get(name)
            if value is None:
                raise PodEngineAPIError(
                    message=(f'Missing required path parameter "{name}" for {descriptor.method} {descriptor.path}.'),
                    status=0,
                    method=descriptor.method,
                    url=self._base_url + path,
                )
            path = path.replace("{" + name + "}", quote(str(value), safe=""))

        query: dict[str, Any] = {}
        for name in descriptor.query_params:
            if all_params.get(name) is not None:
                query[name] = all_params[name]
        query_items = build_query_params(query)

        has_body = descriptor.body != "none"
        json_body: Any = None
        if descriptor.body == "merge":
            json_body = to_jsonable(
                {
                    key: value
                    for key, value in all_params.items()
                    if key not in path_param_set and key not in query_param_set
                }
            )
        elif descriptor.body == "field":
            json_body = to_jsonable(all_params.get("body"))

        headers = {
            "Authorization": self._api_key,
            "x-source": self._source,
            **self._headers,
            **((options.headers if options else None) or {}),
        }
        if has_body:
            headers["Content-Type"] = "application/json"

        return _Prepared(
            method=descriptor.method,
            url=f"{self._base_url}{path}",
            params=query_items,
            json_body=json_body,
            has_body=has_body,
            headers=headers,
            binary=descriptor.binary,
        )

    def _decode_success(self, response: httpx.Response, prepared: _Prepared) -> Any:
        if prepared.binary:
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        text = response.text
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise PodEngineAPIError(
                message=(
                    f"Expected a JSON response but the body could not be parsed "
                    f"(status {response.status_code}): {text[:200]}"
                ),
                status=response.status_code,
                method=prepared.method,
                url=prepared.url,
                body=text,
                request_id=response.headers.get("x-request-id"),
            ) from err
        # Unwrap the ``{ status, data }`` envelope the API uses for JSON responses.
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _api_error(self, response: httpx.Response, prepared: _Prepared) -> PodEngineAPIError:
        raw = response.text
        body: Any = None
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
        return PodEngineAPIError(
            message=extract_error_message(body, f"Request failed with status {response.status_code}"),
            status=response.status_code,
            method=prepared.method,
            url=prepared.url,
            body=body,
            request_id=response.headers.get("x-request-id"),
        )

    def _request_kwargs(self, prepared: _Prepared, timeout: float | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "method": prepared.method,
            "url": prepared.url,
            "params": prepared.params,
            "headers": prepared.headers,
            "timeout": timeout,
        }
        if prepared.has_body:
            kwargs["json"] = prepared.json_body
        return kwargs


class PodEngineCore(_BaseCore):
    """Synchronous transport backed by ``httpx.Client``."""

    def __init__(self, *, http_client: httpx.Client | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = http_client or httpx.Client()
        self._owns_client = http_client is None

    def request(
        self,
        descriptor: EndpointDescriptor,
        params: dict[str, Any] | None = None,
        options: RequestOptions | None = None,
    ) -> Any:
        prepared = self._prepare(descriptor, params, options)
        max_retries = options.max_retries if options and options.max_retries is not None else self._max_retries
        timeout = options.timeout if options and options.timeout is not None else self._timeout

        deadline = self._deadline(options)
        attempt = 0
        while True:
            remaining = self._remaining(deadline, prepared)
            attempt_timeout = (
                min(timeout, remaining) if timeout is not None else (remaining if math.isfinite(remaining) else None)
            )
            try:
                with self._client.stream(**self._request_kwargs(prepared, attempt_timeout)) as response:
                    self._remaining(deadline, prepared)
                    if not response.is_success and _is_retriable_status(response.status_code) and attempt < max_retries:
                        attempt += 1
                        delay = self._retry_delay(response, attempt, deadline, prepared)
                    else:
                        if isinstance(response.stream, httpx.SyncByteStream):
                            response.stream = _DeadlineStream(
                                response.stream, lambda: self._remaining(deadline, prepared)
                            )
                        response.read()
                        self._remaining(deadline, prepared)
                        if not response.is_success:
                            raise self._api_error(response, prepared)
                        result = self._decode_success(response, prepared)
                        self._remaining(deadline, prepared)
                        return result
            except httpx.TransportError as err:
                self._remaining(deadline, prepared)
                if attempt >= max_retries:
                    raise PodEngineConnectionError(
                        message=f"Unable to reach the Pod Engine API at {prepared.url}. {err}",
                        method=prepared.method,
                        url=prepared.url,
                        cause=err,
                    ) from err
                attempt += 1
                delay = self._retry_delay(None, attempt, deadline, prepared)
            # Chunk large server delays to avoid platform time.sleep overflow.
            wake_at = time.monotonic() + delay
            while (wait := wake_at - time.monotonic()) > 0:
                time.sleep(min(wait, 86_400.0))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PodEngineCore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncPodEngineCore(_BaseCore):
    """Asynchronous transport backed by ``httpx.AsyncClient``."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None

    async def request(
        self,
        descriptor: EndpointDescriptor,
        params: dict[str, Any] | None = None,
        options: RequestOptions | None = None,
    ) -> Any:
        deadline = self._deadline(options)
        total = deadline - time.monotonic()
        try:
            return await asyncio.wait_for(
                self._request(descriptor, params, options, deadline), timeout=total if math.isfinite(total) else None
            )
        except asyncio.TimeoutError as err:
            raise self._deadline_error(self._prepare(descriptor, params, options)) from err

    async def _request(
        self,
        descriptor: EndpointDescriptor,
        params: dict[str, Any] | None,
        options: RequestOptions | None,
        deadline: float,
    ) -> Any:
        prepared = self._prepare(descriptor, params, options)
        max_retries = options.max_retries if options and options.max_retries is not None else self._max_retries
        timeout = options.timeout if options and options.timeout is not None else self._timeout

        attempt = 0
        while True:
            remaining = self._remaining(deadline, prepared)
            attempt_timeout = (
                min(timeout, remaining) if timeout is not None else (remaining if math.isfinite(remaining) else None)
            )
            try:
                async with self._client.stream(**self._request_kwargs(prepared, attempt_timeout)) as response:
                    self._remaining(deadline, prepared)
                    if not response.is_success and _is_retriable_status(response.status_code) and attempt < max_retries:
                        attempt += 1
                        delay = self._retry_delay(response, attempt, deadline, prepared)
                    else:
                        await response.aread()
                        self._remaining(deadline, prepared)
                        if not response.is_success:
                            raise self._api_error(response, prepared)
                        result = self._decode_success(response, prepared)
                        self._remaining(deadline, prepared)
                        return result
            except httpx.TransportError as err:
                self._remaining(deadline, prepared)
                if attempt >= max_retries:
                    raise PodEngineConnectionError(
                        message=f"Unable to reach the Pod Engine API at {prepared.url}. {err}",
                        method=prepared.method,
                        url=prepared.url,
                        cause=err,
                    ) from err
                attempt += 1
                delay = self._retry_delay(None, attempt, deadline, prepared)
            await asyncio.sleep(delay)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncPodEngineCore:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
