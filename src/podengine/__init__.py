"""Pod Engine — official Python SDK.

Example:
    from podengine import PodEngine

    pe = PodEngine(api_key="...")
    chart = pe.charts.get_latest_chart(chart_type="apple", country="us", category="top podcasts")

    # Async:
    from podengine import AsyncPodEngine

    pe = AsyncPodEngine(api_key="...")
    chart = await pe.charts.get_latest_chart(chart_type="apple", country="us", category="top podcasts")
"""

from podengine._core._client import RequestOptions
from podengine._core.errors import (
    PodEngineAPIError,
    PodEngineConnectionError,
    PodEngineError,
)
from podengine._generated import models
from podengine._generated.resources import AsyncPodEngine, PodEngine

__version__ = "0.1.0"

__all__ = [
    "PodEngine",
    "AsyncPodEngine",
    "RequestOptions",
    "PodEngineError",
    "PodEngineAPIError",
    "PodEngineConnectionError",
    "models",
    "__version__",
]
