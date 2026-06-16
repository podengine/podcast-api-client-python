"""Unit tests for the pure request/response helpers."""

from __future__ import annotations

import datetime

from podengine._core.transform import (
    build_query_params,
    extract_error_message,
    to_jsonable,
)
from podengine._generated.models import GetLatestChartResponseOptions


def test_build_query_params_repeats_arrays_and_skips_none() -> None:
    items = build_query_params({"id": ["a", "b"], "limit": 10, "skip": None})
    assert ("id", "a") in items
    assert ("id", "b") in items
    assert ("limit", "10") in items
    assert all(k != "skip" for k, _ in items)


def test_build_query_params_lowercases_booleans() -> None:
    items = build_query_params({"enabled": True, "archived": False})
    assert ("enabled", "true") in items
    assert ("archived", "false") in items


def test_build_query_params_renders_datetime_as_iso() -> None:
    when = datetime.datetime(2024, 1, 31, 12, 0, 0, tzinfo=datetime.timezone.utc)
    items = build_query_params({"since": when})
    assert items == [("since", "2024-01-31T12:00:00Z")]


def test_to_jsonable_uses_model_aliases() -> None:
    model = GetLatestChartResponseOptions(chartType="apple", country="us")
    out = to_jsonable(model)
    assert out["chartType"] == "apple"
    assert out["country"] == "us"


def test_to_jsonable_serializes_datetime() -> None:
    when = datetime.datetime(2024, 1, 31, 12, 0, 0, tzinfo=datetime.timezone.utc)
    assert to_jsonable({"at": when}) == {"at": "2024-01-31T12:00:00Z"}


def test_extract_error_message_prefers_nested_data_message() -> None:
    assert extract_error_message({"data": {"message": "boom"}}, "fallback") == "boom"
    assert extract_error_message({"error": "nope"}, "fallback") == "nope"
    assert extract_error_message("raw text", "fallback") == "raw text"
    assert extract_error_message({}, "fallback") == "fallback"
    assert extract_error_message(None, "fallback") == "fallback"
