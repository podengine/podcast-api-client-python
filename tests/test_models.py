"""Structural assertions on the generated pydantic models (robust to spec changes)."""

from __future__ import annotations

import datetime
import json

import pydantic

from podengine._generated import models as m
from podengine._generated.models import GetLatestChartResponseOptions


def _all_models() -> list[type[pydantic.BaseModel]]:
    out = []
    for name in dir(m):
        obj = getattr(m, name)
        if isinstance(obj, type) and issubclass(obj, pydantic.BaseModel) and obj is not pydantic.BaseModel:
            out.append(obj)
    return out


def test_date_time_fields_are_datetime_typed() -> None:
    # Proves the emitter maps `format: date-time` -> datetime (not str).
    count = sum(
        1 for model in _all_models() for field in model.model_fields.values() if "datetime" in str(field.annotation)
    )
    assert count > 0


def test_enums_become_literals() -> None:
    annotated = str(GetLatestChartResponseOptions.model_fields["chart_type"].annotation)
    assert "Literal" in annotated and "apple" in annotated


def test_alias_round_trip_populate_by_name() -> None:
    # Construct by python (snake) name and by wire alias; both accepted, dumped by alias.
    # populate_by_name works at runtime, but type checkers only see the alias as the constructor
    # parameter (dataclass_transform can't model "accept both"), so the by-name call is ignored.
    by_name = GetLatestChartResponseOptions(chart_type="apple", positions_limit=5)  # pyright: ignore[reportCallIssue]
    by_alias = GetLatestChartResponseOptions.model_validate({"chartType": "apple", "positionsLimit": 5})
    assert by_name.chart_type == by_alias.chart_type == "apple"
    assert by_name.model_dump(by_alias=True)["positionsLimit"] == 5


def test_datetime_inbound_parsing() -> None:
    # End-to-end: a model with a datetime field parses an ISO string to a datetime.
    field_model = next(
        (model for model in _all_models() if any("datetime" in str(f.annotation) for f in model.model_fields.values())),
        None,
    )
    assert field_model is not None
    dt_field = next(n for n, f in field_model.model_fields.items() if "datetime" in str(f.annotation))
    # Build a payload with only the datetime field populated by alias; tolerate other required
    # fields via construction of a partial dict is not possible, so validate just the type adapter.
    annotation = field_model.model_fields[dt_field].annotation
    assert annotation is not None
    adapter = pydantic.TypeAdapter(annotation)
    assert isinstance(adapter.validate_python("2024-01-31T12:00:00.000Z"), datetime.datetime)


def test_apple_ids_are_int_not_float() -> None:
    # PE-5008: the spec typed appleId as `number`, so these were floats and Apple links ended in `id315114957.0`.
    apple_id_fields = [
        (model.__name__, name, str(field.annotation))
        for model in _all_models()
        for name, field in model.model_fields.items()
        if field.alias in ("appleId", "notFoundAppleIds")
    ]
    assert apple_id_fields
    assert [f for f in apple_id_fields if "float" in f[2] or "int" not in f[2]] == []


def test_search_hit_apple_id_stays_an_int() -> None:
    # Search hits carry `episode_id`/`score` alongside the parent podcast's `apple_id`.
    hit_models = [model for model in _all_models() if {"apple_id", "episode_id", "score"} <= set(model.model_fields)]
    assert hit_models
    for model in hit_models:
        annotation = model.model_fields["apple_id"].annotation
        assert annotation is not None
        adapter = pydantic.TypeAdapter(annotation)
        for wire in (315114957, "315114957"):  # a number now; older API builds sent a string
            value = adapter.validate_json(json.dumps(wire))
            assert isinstance(value, int) and value == 315114957
            assert f"https://podcasts.apple.com/podcast/id{value}" == "https://podcasts.apple.com/podcast/id315114957"
        assert adapter.validate_json("null") is None
