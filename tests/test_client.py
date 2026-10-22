"""End-to-end wiring tests for representative generated resource methods (sync + async)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from podengine import NULL, AsyncPodEngine, PodEngine, PodEngineAPIError
from podengine._generated.models import (
    GetAlternativeSpellingsResponse,
    GetLatestChartResponse,
    GetTranscriptPassagesSearchOptions,
)

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
def test_unset_body_arguments_are_left_out(respx_mock: respx.MockRouter) -> None:
    # PE-5027: unset arguments went on the wire as null, and the API rejects null on optional fields.
    route = respx_mock.post(f"{BASE}/api/v1/search/episodes").mock(
        return_value=httpx.Response(400, json={"status": "error", "message": "rejected"})
    )
    pe = PodEngine("k", http_client=httpx.Client(), max_retries=0)
    # Plain dicts work on the wire; the generated hints only name the models.
    term: Any = {"searchTerm": '"postpartum depression"', "searchType": "text", "searchTargets": ["transcript"]}
    with pytest.raises(PodEngineAPIError):  # the body is what's under test, not the response
        pe.search.search_episodes(page_size=10, search_terms=[term], published_since="2026-10-21")
    assert json.loads(route.calls.last.request.content) == {
        "pageSize": 10,
        "searchTerms": [term],
        "publishedSince": "2026-10-21",
    }


@respx.mock
def test_null_sentinel_sends_explicit_null(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.patch(f"{BASE}/api/v1/projects/my-project/podcasts/lp1").mock(
        return_value=httpx.Response(400, json={"status": "error", "message": "rejected"})
    )
    pe = PodEngine("k", http_client=httpx.Client(), max_retries=0)
    with pytest.raises(PodEngineAPIError):  # the body is what's under test, not the response
        pe.projects.update_project_listed_podcast(project_slug="my-project", listed_podcast_id="lp1", priority=NULL)
    # `state` was not passed, so it is not sent (and not cleared); `priority` is cleared.
    assert json.loads(route.calls.last.request.content) == {"priority": None}


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


@respx.mock
def test_transcript_passage_count_and_cursor(respx_mock: respx.MockRouter) -> None:
    data = {
        "episodeId": "ep",
        "bestPassage": {"id": "best", "text": "<em>match</em>"},
        "passages": [],
        "total": {"value": 101, "relation": "gte"},
        "additionalCount": 100,
        "truncated": True,
        "hasMore": False,
        "nextCursor": None,
        "passageLimit": 100,
    }
    route = respx_mock.post(f"{BASE}/api/v1/search/episodes/ep/transcript").mock(
        return_value=httpx.Response(200, json={"status": "OK", "data": data})
    )
    pe = PodEngine("k", http_client=httpx.Client())
    result = pe.search.get_transcript_passages(
        episode_id="ep",
        search_options=GetTranscriptPassagesSearchOptions.model_validate(
            {
                "searchTerms": [
                    {
                        "searchType": "text",
                        "searchTerm": "match",
                        "searchTargets": ["transcript"],
                        "searchTermOptions": {"matchMode": "must"},
                    }
                ]
            }
        ),
        page_size=5,
        cursor="opaque",
    )
    body = json.loads(route.calls.last.request.content)
    assert "episodeId" not in body
    assert body["cursor"] == "opaque"
    assert body["searchOptions"]["searchTerms"][0]["searchTerm"] == "match"
    assert result.total.relation == "gte"
    assert result.additional_count == 100
    assert result.has_more is False
