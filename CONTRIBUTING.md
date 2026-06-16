# Contributing

Thanks for your interest in the Pod Engine Python SDK!

## This repository is generated

This repo is an **automatically-synced mirror**. The SDK is generated from the Pod Engine API's
OpenAPI specification in our internal monorepo, which is the source of truth. The published
`podengine` PyPI package is released from here, but the source is synced automatically —
**direct commits to mirrored files are overwritten on the next sync.**

## How to help

- **Found a bug or have a feature request?** Please
  [open an issue](https://github.com/podengine/podcast-api-client-python/issues). We read every one.
- **Want to fix something in code?** Open an issue describing the change first. Because the code
  here is generated and synced, we'll land the fix on our side so it survives regeneration.
- **Spotted a wrong type or a missing endpoint?** That almost always comes from the OpenAPI spec —
  mention it in the issue and we'll correct it at the source.

## Running locally

```bash
uv sync                                      # create .venv and install deps
bun run scripts/generate-python-client.ts    # regenerate the typed client from openapi.json
uv run ruff format src                        # normalize generated output
uv run pytest
uv build
```

(Regeneration uses [Bun](https://bun.sh) because the code generator is shared with the
TypeScript SDK to guarantee both clients stay in lockstep.)

Thanks for helping make the SDK better! 🎙️
