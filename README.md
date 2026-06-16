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
    timeout=60.0,                          # per-request timeout (seconds)
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
    request_options=RequestOptions(timeout=10.0, max_retries=0),
)
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
