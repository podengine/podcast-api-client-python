# Pod Engine Python SDK

Official Python client for the [Pod Engine](https://www.podengine.ai) podcast intelligence
API — search 4M+ podcasts, fetch metadata, transcripts, charts, guest profiles and more.

The client is **fully generated** from Pod Engine's published OpenAPI specification, so it
stays in lockstep with the API and ships typed [pydantic](https://docs.pydantic.dev) models
for every request and response.

## Install

```bash
pip install podengine
# or: uv add podengine
```

Requires Python 3.10+.

## Quickstart (sync)

```python
from podengine import PodEngine

pe = PodEngine(api_key="YOUR_API_KEY")

chart = pe.charts.get_latest_chart(chart_type="apple", country="us", category="top podcasts")
for entry in chart.podcasts:
    print(entry.rank, entry.title)

results = pe.search.search_podcasts(
    search_terms=[
        {
            "searchTerm": "startups",
            "searchType": "text",
            "searchTargets": ["podcast-title"],
            "searchTermOptions": {"matchMode": "optional"},
        }
    ],
)
```

Get an API key at <https://www.podengine.ai/get-started>.

## Quickstart (async)

```python
import asyncio
from podengine import AsyncPodEngine

async def main() -> None:
    async with AsyncPodEngine(api_key="YOUR_API_KEY") as pe:
        chart = await pe.charts.get_latest_chart(
            chart_type="apple", country="us", category="top podcasts"
        )
        print(chart)

asyncio.run(main())
```

## Configuration

```python
pe = PodEngine(
    api_key="YOUR_API_KEY",
    base_url="https://api.podengine.ai",  # override for staging; or set PODENGINE_API_URL
    source="my-app",                       # sent as the x-source header
    timeout=60.0,                          # HTTPX per-attempt I/O timeout (seconds)
    total_timeout=60.0,                    # overall request budget (seconds)
    max_retries=2,                         # transient 429/5xx/network retries
)
```

Per-call overrides are available via `RequestOptions`:

```python
from podengine import RequestOptions

chart = pe.charts.get_latest_chart(
    chart_type="apple",
    country="us",
    category="top podcasts",
    request_options=RequestOptions(timeout=10.0, total_timeout=15.0, max_retries=0),
)
```

For apps, agents and data pipelines, `total_timeout` sets a budget for attempts, retry waits and response reads. Both clients default to 60 seconds. Set a positive number on the client or a single call to change it. Set `0` to disable it; client-level `None` also disables it. In `RequestOptions`, `None` inherits the client setting. The existing `timeout` option keeps its HTTPX per-attempt I/O behavior.

A valid `Retry-After` sets the minimum retry wait, with 1–251 ms of added jitter. If that wait cannot fit within the remaining budget, the call raises `PodEngineConnectionError` immediately. It does not retry early. Expired budgets also raise `PodEngineConnectionError`.

The async client enforces the deadline across the complete call and propagates task cancellation. The sync client caps each HTTPX timeout to the remaining budget and checks the deadline between streamed response reads. It cannot interrupt synchronous I/O already in progress, so a blocking read or custom transport can return after the deadline. No further retry starts after the budget expires.

## Leaving out and clearing fields

Arguments you don't pass are left out of the request. To set a field to `null` on purpose, such as clearing a listed podcast's priority, pass `NULL`:

```python
from podengine import NULL

pe.projects.update_project_listed_podcast(project_slug="my-project", listed_podcast_id="lp_123", priority=NULL)
```

## Error handling

Every failure is a `PodEngineError` subclass:

```python
from podengine import PodEngine, PodEngineAPIError, PodEngineConnectionError

pe = PodEngine(api_key="YOUR_API_KEY")
try:
    chart = pe.charts.get_latest_chart(chart_type="apple", country="us", category="top podcasts")
except PodEngineAPIError as err:
    print(err.status, err.message, err.request_id)  # non-2xx response
except PodEngineConnectionError as err:
    print("network failure:", err)                  # no HTTP response
```

## License

MIT
