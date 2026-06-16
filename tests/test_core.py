"""Transport-core tests using synthetic endpoint descriptors and a mocked httpx client.

These exercise the hand-written core directly (independent of any specific generated
operation) so they stay valid as the spec evolves.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from podengine import PodEngineAPIError, PodEngineConnectionError, RequestOptions
from podengine._core import _client
from podengine._core._client import EndpointDescriptor, PodEngineCore

BASE = "https://api.podengine.ai"

GET = EndpointDescriptor(
    method="GET", path="/api/v1/thing/{id}", path_params=("id",), query_params=("q", "tags"), body="none", binary=False
)
GET_PLAIN = EndpointDescriptor(
    method="GET", path="/api/v1/plain", path_params=(), query_params=(), body="none", binary=False
)
POST_MERGE = EndpointDescriptor(
    method="POST", path="/api/v1/thing/{id}", path_params=("id",), query_params=("q",), body="merge", binary=False
)
POST_FIELD = EndpointDescriptor(
    method="POST", path="/api/v1/bulk", path_params=(), query_params=(), body="field", binary=False
)
GET_BINARY = EndpointDescriptor(
    method="GET", path="/api/v1/download", path_params=(), query_params=(), body="none", binary=True
)


def make_core(**kwargs: object) -> PodEngineCore:
    kwargs.setdefault("api_key", "secret-key")
    return PodEngineCore(http_client=httpx.Client(), **kwargs)  # type: ignore[arg-type]


def test_missing_api_key_raises() -> None:
    with pytest.raises(PodEngineAPIError):
        PodEngineCore(api_key="")


@respx.mock
def test_auth_and_source_headers(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{BASE}/api/v1/plain").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"ok": True}})
    )
    core = make_core(source="my-app", headers={"x-extra": "1"})
    core.request(GET_PLAIN, {}, RequestOptions(headers={"x-call": "2"}))
    req = route.calls.last.request
    # Raw API key, NOT a Bearer token (matches the API + docs).
    assert req.headers["authorization"] == "secret-key"
    assert req.headers["x-source"] == "my-app"
    assert req.headers["x-extra"] == "1"
    assert req.headers["x-call"] == "2"


@respx.mock
def test_path_params_and_query(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(url__regex=rf"{BASE}/api/v1/thing/.*").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": 1})
    )
    core = make_core()
    core.request(GET, {"id": "a/b", "q": "hi", "tags": ["x", "y"]})
    req = route.calls.last.request
    assert "/api/v1/thing/a%2Fb" in str(req.url)  # path value URL-encoded
    assert req.url.params.get_list("tags") == ["x", "y"]
    assert req.url.params["q"] == "hi"


def test_missing_path_param_raises() -> None:
    core = make_core()
    with pytest.raises(PodEngineAPIError, match="Missing required path parameter"):
        core.request(GET, {"q": "x"})


@respx.mock
def test_body_merge_excludes_path_and_query(respx_mock: respx.MockRouter) -> None:
    import json

    route = respx_mock.post(url__regex=rf"{BASE}/api/v1/thing/.*").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {}})
    )
    core = make_core()
    core.request(POST_MERGE, {"id": "p1", "q": "search", "name": "n", "count": 3})
    body = json.loads(route.calls.last.request.content)
    assert body == {"name": "n", "count": 3}  # path 'id' + query 'q' excluded


@respx.mock
def test_body_field_passthrough(respx_mock: respx.MockRouter) -> None:
    import json

    route = respx_mock.post(f"{BASE}/api/v1/bulk").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {}})
    )
    core = make_core()
    core.request(POST_FIELD, {"body": ["a", "b", "c"]})
    assert json.loads(route.calls.last.request.content) == ["a", "b", "c"]


@respx.mock
def test_envelope_unwrap_and_passthrough(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/v1/plain").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"x": 1}})
    )
    assert make_core().request(GET_PLAIN, {}) == {"x": 1}

    respx_mock.get(f"{BASE}/api/v1/plain").mock(return_value=httpx.Response(200, json={"x": 2}))
    assert make_core().request(GET_PLAIN, {}) == {"x": 2}  # no envelope -> returned as-is


@respx.mock
def test_binary_returns_bytes(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/v1/download").mock(return_value=httpx.Response(200, content=b"\x00\x01rawbytes"))
    out = make_core().request(GET_BINARY, {})
    assert out == b"\x00\x01rawbytes"


@respx.mock
def test_204_and_empty_body_return_none(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/v1/plain").mock(return_value=httpx.Response(204))
    assert make_core().request(GET_PLAIN, {}) is None


@respx.mock
def test_non_json_2xx_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/v1/plain").mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(PodEngineAPIError, match="could not be parsed"):
        make_core().request(GET_PLAIN, {})


@respx.mock
def test_api_error_carries_status_message_request_id(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/v1/plain").mock(
        return_value=httpx.Response(404, json={"message": "not found"}, headers={"x-request-id": "req_9"})
    )
    with pytest.raises(PodEngineAPIError) as exc:
        make_core(max_retries=0).request(GET_PLAIN, {})
    assert exc.value.status == 404
    assert exc.value.message == "not found"
    assert exc.value.request_id == "req_9"
    assert exc.value.method == "GET"


@respx.mock
def test_connection_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/v1/plain").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(PodEngineConnectionError):
        make_core(max_retries=0).request(GET_PLAIN, {})


@respx.mock
def test_retry_429_then_success(respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_client, "_backoff_seconds", lambda attempt: 0)
    route = respx_mock.get(f"{BASE}/api/v1/plain")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(200, json={"status": "success", "data": "ok"}),
    ]
    assert make_core(max_retries=2).request(GET_PLAIN, {}) == "ok"
    assert route.call_count == 2


@respx.mock
def test_retry_5xx_exhausts(respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_client, "_backoff_seconds", lambda attempt: 0)
    route = respx_mock.get(f"{BASE}/api/v1/plain").mock(return_value=httpx.Response(503))
    with pytest.raises(PodEngineAPIError) as exc:
        make_core(max_retries=2).request(GET_PLAIN, {})
    assert exc.value.status == 503
    assert route.call_count == 3  # initial + 2 retries


@respx.mock
def test_no_retry_on_4xx(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{BASE}/api/v1/plain").mock(return_value=httpx.Response(400, json={"message": "bad"}))
    with pytest.raises(PodEngineAPIError):
        make_core(max_retries=3).request(GET_PLAIN, {})
    assert route.call_count == 1  # 400 is not retriable


@respx.mock
def test_per_call_max_retries_override(respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_client, "_backoff_seconds", lambda attempt: 0)
    route = respx_mock.get(f"{BASE}/api/v1/plain").mock(return_value=httpx.Response(500))
    with pytest.raises(PodEngineAPIError):
        make_core(max_retries=5).request(GET_PLAIN, {}, RequestOptions(max_retries=0))
    assert route.call_count == 1


def test_base_url_env_and_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PODENGINE_API_URL", "https://staging.podengine.ai/")
    core = PodEngineCore(api_key="k")
    assert core._base_url == "https://staging.podengine.ai"
    core2 = PodEngineCore(api_key="k", base_url="https://override.example/")
    assert core2._base_url == "https://override.example"
