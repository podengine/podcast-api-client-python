"""Error types raised by the Pod Engine SDK.

Every failure surfaces as a :class:`PodEngineError` subclass so consumers can ``except``
a single base type and narrow with ``isinstance`` when they care about the specifics.
"""

from __future__ import annotations

from typing import Any


class PodEngineError(Exception):
    """Base class for every error raised by the SDK."""


class PodEngineAPIError(PodEngineError):
    """The API returned a non-2xx response.

    Carries the HTTP status, the request URL/method, and a best-effort parsed error
    message / raw body for debugging.
    """

    def __init__(
        self,
        *,
        message: str,
        status: int,
        method: str,
        url: str,
        body: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.method = method
        self.url = url
        #: Raw parsed response body, when available (decoded JSON or text).
        self.body = body
        #: Value of the ``x-request-id`` response header, when present.
        self.request_id = request_id


class PodEngineConnectionError(PodEngineError):
    """The request never produced an HTTP response.

    DNS failure, connection refused, or a timeout — there is no status code.
    """

    def __init__(
        self,
        *,
        message: str,
        method: str,
        url: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.method = method
        self.url = url
        self.cause = cause
