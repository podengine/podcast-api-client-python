"""Guards that the committed generated client matches the committed openapi.json:
every spec operation has exactly one descriptor and one method on each client surface.
"""

from __future__ import annotations

import json
from pathlib import Path

from podengine import AsyncPodEngine, PodEngine
from podengine._generated.resources import _DESCRIPTORS

PKG_ROOT = Path(__file__).resolve().parent.parent
LOCAL_SPEC = PKG_ROOT / "openapi.json"
SIBLING_SPEC = PKG_ROOT.parent / "podcast-api-client-js" / "openapi.json"
SPEC_PATH = LOCAL_SPEC if LOCAL_SPEC.exists() else SIBLING_SPEC

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _spec_operation_ids() -> set[str]:
    spec = json.loads(SPEC_PATH.read_text())
    ids: set[str] = set()
    for path_item in spec["paths"].values():
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if op and "operationId" in op:
                ids.add(op["operationId"])
    return ids


def _public_methods(instance: object) -> list[str]:
    methods: list[str] = []
    for attr in vars(instance):
        if attr.startswith("_"):
            continue
        resource = getattr(instance, attr)
        for name in dir(type(resource)):
            if not name.startswith("_") and callable(getattr(resource, name)):
                methods.append(f"{attr}.{name}")
    return methods


def test_descriptors_match_spec_operations() -> None:
    assert set(_DESCRIPTORS.keys()) == _spec_operation_ids()


def test_every_operation_has_one_sync_method() -> None:
    methods = _public_methods(PodEngine("k"))
    assert len(methods) == len(methods_set := set(methods))  # no duplicates
    assert len(methods_set) == len(_DESCRIPTORS)


def test_sync_and_async_surfaces_match() -> None:
    assert sorted(_public_methods(PodEngine("k"))) == sorted(_public_methods(AsyncPodEngine("k")))
