"""End-to-end wiring tests for representative generated resource methods (sync + async)."""

from __future__ import annotations

import asyncio
import json

import httpx
import respx

from podengine import AsyncPodEngine, PodEngine
from podengine._generated.models import GetAlternativeSpellingsResponse, GetLatestChartResponse

BASE = "https://api.podengine.ai"


@respx.mock
def test_get_latest_chart_query_envelope_and_model(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{BASE}/api/v1/charts/latest").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"options": {"chartType": "apple"}, "chart": None}}
        )
    )
    pe = PodEngine("secret-key", http_client=httpx.Client())
    res = pe.charts.get_latest_chart(chart_type="apple", country="us", category="top podcasts", positions_limit=10)
    assert isinstance(res, GetLatestChartResponse)
    assert res.options.chart_type == "apple"

    params = route.calls.last.request.url.params
    assert params["chartType"] == "apple"
    assert params["country"] == "us"
    assert params["category"] == "top podcasts"
    assert params["positionsLimit"] == "10"


@respx.mock
def test_post_body_method(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/api/v1/agent/alternative-spellings").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"spellings": ["color", "colour"]}})
    )
    pe = PodEngine("k", http_client=httpx.Client())
    res = pe.agent.get_alternative_spellings(value="color")
    assert isinstance(res, GetAlternativeSpellingsResponse)
    assert res.spellings == ["color", "colour"]
    assert json.loads(route.calls.last.request.content) == {"value": "color"}


@respx.mock
def test_path_param_and_binary_download(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(url__regex=rf"{BASE}/api/v1/episodes/.*/download/transcript").mock(
        return_value=httpx.Response(200, content=b"WEBVTT\n\nhello")
    )
    pe = PodEngine("k", http_client=httpx.Client())
    out = pe.episodes.download_episode_transcript(episode_id="ep 1/2", format="vtt")
    assert out == b"WEBVTT\n\nhello"
    assert "/api/v1/episodes/ep%201%2F2/download/transcript" in str(route.calls.last.request.url)
    assert route.calls.last.request.url.params["format"] == "vtt"


def test_async_round_trip() -> None:
    async def go() -> None:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/api/v1/charts/latest").mock(
                return_value=httpx.Response(200, json={"status": "success", "data": {"options": {}, "chart": None}})
            )
            async with AsyncPodEngine("k", http_client=httpx.AsyncClient()) as pe:
                res = await pe.charts.get_latest_chart(chart_type="apple", country="us", category="top")
                assert isinstance(res, GetLatestChartResponse)

    asyncio.run(go())
