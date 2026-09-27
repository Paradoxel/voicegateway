# AGENTS.md

This file provides guidance to coding agents (Codex, Claude Code, and others) when working with code in this repository.

## Project

VoiceGateway: cost tracking and reconciliation for voice agents on LiveKit, Pipecat and OpenRTC. Meters the native STT, LLM, and TTS instances you pass to `attach()` / `guard()` across cloud providers (OpenAI, Deepgram, Anthropic, Groq, Cartesia, ElevenLabs, AssemblyAI) and local models (Whisper, Kokoro, Piper). LLM, STT, and TTS prices all flow through `voice-prices` (a fork of `pydantic/genai-prices`). Ships `voicegw reconcile` for verifying recorded numbers against provider invoices, plus per-modality cost tracking, resolver-time fallback chains, rate limiting, and a web dashboard.

## Commands

```bash
# Install (editable, with dev dependencies)
pip install -e ".[dev]"

# Run tests
pytest
pytest src/voicegateway/tests/core/test_config.py              # single file
pytest src/voicegateway/tests/core/test_config.py::test_name   # single test
pytest --cov                                                   # with coverage

# CLI
voicegw init                             # create config template
voicegw serve --port 8080                # start HTTP API
voicegw dashboard                        # open the web UI served by `voicegw serve`
voicegw status                           # show provider status

# Dashboard frontend (src/dashboard/frontend/)
npm install && npm run dev               # dev server
npm run build                            # production build

# Docker
docker compose up -d                     # API + Dashboard
docker compose --profile local up -d     # + Ollama
```

## Architecture

**Metering flow:** `voicegateway.attach(session)` detects the framework (LiveKit `AgentSession` or Pipecat `PipelineTask`) without importing it eagerly, subscribes to its metrics events, and `MetricCapture` turns each STT/LLM/TTS metric into a `RequestRecord` (audio seconds, tokens, characters). `inference/pricing/` prices it through `voice-prices`, then a `Sink` writes it: embedded SQLite by default, a remote collector in fleet mode, or ClickHouse. The dashboard and `/v1/*` read what the sinks stored. `guard()` wraps a single provider for fallback, rate limit and budget and writes no metrics.

**Metering (`src/voicegateway/inference/`):**
- `session/attach.py`: `attach()`, framework detection, turn, dead-air, tool-call, transcript and snapshot capture; `session/policy.py` holds the named capture policies
- `session/capture.py`: `MetricCapture`, converts framework metrics to priced records
- `livekit/`, `pipecat/`: framework-specific `guard()` implementations and the Pipecat `Observer`
- `pricing/`: `calculate_cost_detail()` dispatches by modality to `voice-prices`; self-hosted `local/*` and `ollama/*` price at $0, catalogue matches without a rate are tagged `voice-prices-unrated`
- `providers/`: 11 `BaseProvider` classes, registered in `core/registry.py`. They back the server's provider management and status endpoints, not the metering path.

**Core (`src/voicegateway/core/`):**
- `gateway.py`: `Gateway`, a shared-state container (config, rate card, cost tracker, rate limiter, budget enforcer, storage) for the server, CLI and MCP; not a request router
- `config.py`: YAML parser with `${ENV_VAR}` substitution
- `container.py`, `app_wiring.py`: dependency-injector and SQLAlchemy wiring for the FastAPI app
- `provider_names.py`: canonical provider ids, resolved against the `voice-prices` catalog

**Accounting and billing:** `accounting/` holds versioned, strict wire contracts (decimal-string money) and `AccountingOutbox`, a restart-safe store-and-forward queue to a collector's `/v1/accounting/usage`. `billing/` holds the rate card, rating, and margin reconciliation.

**Middleware (`src/voicegateway/middleware/`):** cost tracking, latency monitoring, rate limiting, budget enforcement, turn tracking, dead-air detection, replay capture, and background workers (node samples, latency and agent observations).

**Storage:** SQLModel models in `models/`, repositories in `repository/`, services in `services/` (`storage_service.py` is the SQLite facade, `sinks.py` the write seam). Alembic migrations live in `alembic/` at the repo root and define the `daily_costs` and `project_daily_costs` views. Optional ClickHouse support lives in `clickhouse/`.

**HTTP API (`src/voicegateway/server/main.py`):** one FastAPI app mounting the system router (`/health`), the `/v1/*` router (`server/api/`: costs, projects, logs, metrics, models, providers, accounting, ingest, sessions, and more), the dashboard router, and the openorca router. The MCP server behind `voicegw mcp` lives in `server/mcp/`.

**Dashboard API (`/api/*`):** served by the same combined server, not a separate process. `server/routes.py` builds `dashboard_router = APIRouter(prefix="/api")` from `server/api/dashboard/` and `server/main.py` includes it. Read endpoints live here under `require_principal`; `/v1/*` above carries reads as well as every write and ingest route. The standalone dashboard FastAPI at `src/dashboard/api/main.py` was deleted in 2026-05: the routes moved, they did not go away.

**Dashboard UI (`src/dashboard/`):** two SPAs plus branding assets. `frontend/` is the React/TypeScript/Vite dashboard (Recharts, Neo-Brutalism aesthetic); `console/` is a smaller SPA built on `@openorca-ui/react`. `api/` now holds only `static/branding/` images and no Python. The combined server serves the built SPA at `/` (see `server/static.py`).

**Docs:** The documentation site (<https://docs.voicegateway.dev>) is a Fumadocs app in `site/docs/` (Next.js static export served from Cloudflare via `site/docs/wrangler.jsonc`). Pages are MDX in `site/docs/content/docs/`, ordered by `meta.json`; brand assets in `site/docs/public/assets/`. The shared palette is `site/theme.css`. Keep it small: six pages while the project is early. Voice: short declarative sentences, sentence-case headings, no em dashes (CI fails on one). Docs version with the code: change them in the same PR as any behavior or API change. Contributor guides live in `contributing/`.

**Landing page:** <https://voicegateway.dev> is an Astro app in `site/web/` on Cloudflare Workers: prerendered pages plus `/api/waitlist` and `/api/stats` (Cloudflare D1) and the `/demo` dashboard build. It shares `site/theme.css` with the docs. `collector.sh` and `install.sh` at the repo root are copied into it at build, so the published scripts cannot drift. It replaces the old `mahimairaja/voicegateway-web` repo.

**Public API:** `voicegateway/__init__.py` exports `attach`, `guard`, `Observer`, `register_worker`, `inference`, `__version__`. There is no `Gateway` / `ModelId` factory surface: it was removed in the framework-agnostic reshape, and metering now happens by wrapping instances you construct.

## Key Patterns

- **Async throughout** — all DB, HTTP, and provider operations use async/await
- **Framework-agnostic**: install provider plugins in your own agent (livekit.plugins.* / pipecat.services.*), not as VoiceGateway extras. VG meters the native instances you pass to attach()/guard() and prices by model_id via voice-prices.
- **Config format** — YAML at `voicegw.yaml`, env vars via `${VAR_NAME}` syntax
- **pytest-asyncio** — `asyncio_mode = "auto"` in pyproject.toml, no manual `@pytest.mark.asyncio` needed
- **Test fixtures** in `src/voicegateway/tests/conftest.py` set fake API keys for all providers
