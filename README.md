# AMRP — Agentic Multi-Market Research Platform

[![CI](https://github.com/LeelaissakAttota/agentic-equity-research-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/LeelaissakAttota/agentic-equity-research-platform/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**AMRP (formerly the Agentic Equity Research Platform, AERP) is an evidence-first research platform for Indian (NSE/BSE) and US (NASDAQ/NYSE) equities, with dashboard pages for commodities, forex and crypto: it resolves a company, plans bounded research, gathers time-aware evidence, verifies claims, and produces traceable JSON / Markdown / Word reports — with free-first LLM use (paid models only by explicit opt-in) and zero-cost defaults.**

## Quick start

```bash
python -m pip install -r requirements-lock.txt && python -m pip install -e . --no-deps
python -m pytest
python -m uvicorn financial_intelligence.main:app --reload   # then open http://127.0.0.1:8000/docs
```

## Dashboard (simple web UI)

A small dashboard sits on top of the existing REST and control APIs (no backend rewrite).

```powershell
# Windows, from the repo root (installs the dashboard requirement once)
.\scripts\start-dashboard.ps1        # opens http://127.0.0.1:8501 ; Ctrl+C stops everything
```

It starts the REST API (`:8000`), the control API (`:8712`, random per-session token, no
password to type) and the dashboard (`:8501`), all on `127.0.0.1`. Logs go to `logs/`.

| Page | What you get |
|---|---|
| **Research** | One box: type a company and a question. The company and the research objective are detected automatically; results appear on their own: analyst summary, key figures, news, regulatory items, Markdown download. Example chips: Apple, Reliance, TCS, Tata Motors demerger, Comprehensive research. Several matching companies (e.g. after the Tata Motors demerger) are shown as buttons to pick. |
| Approval | Comprehensive research shows an **Approval needed** banner with Approve / Reject; nothing runs until you approve. |
| **Step-by-step** | Every research step waits for Approve / Reject, failed or rejected steps can be retried, with a live log (control API). |
| **Settings** | Guided provider setup (OpenRouter, OpenAI, Anthropic, Gemini, Groq, custom OpenAI-compatible): the fields each needs, **Test connection**, **Save** (write-only, only the last 4 characters are shown). Automatic model choice preview (free first; shows why). Live-data toggles (applied on next start). |
| **Health** | Version, provider health and recent research runs. |

Every section carries a badge: **LIVE**, **LIVE (cached)**, **SAMPLE DATA** or **NOT AVAILABLE**.

Notes: research summaries still use **free OpenRouter models only** (`ALLOW_PAID_MODELS=false`,
see `MODEL_POLICY.md`); keys for the other providers are stored for later use. The UI helpers live
in `src/financial_intelligence/dashboard/` (stdlib only, tested); the UI itself is `dashboard/app.py`.

## Live vs sample data

Everything runs offline on bundled, explicitly labelled fixture data by default. Live adapters are opt-in.

| Capability | Default (offline) | Live option | Enable with |
|---|---|---|---|
| Company identity | Bundled reference catalog | + full BSE equity list and SEC ticker list (renames/demergers mapped, e.g. `TATAMOTORS -> TMPV`, `ZOMATO -> ETERNAL`) | `COMPANY_UNIVERSE_*` (follows the live market/financial flags) |
| Market quotes/history | Fixture dataset | Yahoo Finance chart (no key) → Alpha Vantage (optional key) → fixture; NSE/BSE live quote overlay for Indian listings | `MARKET_DATA_LIVE_ENABLED=true`, optional `ALPHA_VANTAGE_*` |
| Financials (US) | Fixture dataset | SEC EDGAR XBRL company facts (current + prior FY) | `FINANCIAL_DATA_LIVE_ENABLED=true` + `SEC_USER_AGENT` (real email) |
| Financials (India) | Fixture sample (Reliance only, labelled) | Yahoo fundamentals timeseries (no key, unofficial Tier 2) | `FINANCIAL_DATA_LIVE_ENABLED=true` (`INDIA_FUNDAMENTALS_LIVE_ENABLED`) |
| News & events | Fixture dataset | GDELT, Google News RSS, IR feeds, NSE/BSE announcements | `LIVE_NEWS_ENABLED`, `INDIA_FILINGS_LIVE_ENABLED` |
| Industry & competitors | Fixture dataset | — | — |
| Regulatory events | Fixture dataset | SEC litigation/admin RSS, SEBI RSS | `LIVE_REGULATORY_ENABLED=true` |
| Research planner | Deterministic | Free OpenRouter model | `PLANNER_MODE=llm` + `OPENROUTER_LIVE_ENABLED=true` |
| Report analyst summary | Deterministic summary | Free OpenRouter model, cited `[F1]`/`[N1]` evidence | `OPENROUTER_LIVE_ENABLED=true` + `OPENROUTER_API_KEY` |
| Verification / synthesis | Deterministic | — | — |

Key live-mode settings (see `.env.example` for all of them):

- `SEC_USER_AGENT="Your Name you@example.com"` — required by SEC fair-access; use a real
  mailbox (SEC returns 403 for GitHub `noreply` addresses). Without it, live SEC data stays off.
- `PRIMARY_FREE_MODEL=auto` — discovers OpenRouter `:free` models, probes them, caches the
  ranking (`OPENROUTER_FREE_MODEL_CACHE_*`) and falls back to the next model on failure.
- `ALPHA_VANTAGE_API_KEY` (+ `ALPHA_VANTAGE_DAILY_LIMIT`, default 25/day) — optional
  second market source with a persisted daily request budget.
- `API_DOCS_ENABLED` — Swagger `/docs` and `/redoc`; on by default in development.
- `GET /health/providers` — last success/failure per live source (Yahoo, NSE, BSE, SEC,
  Alpha Vantage, company universe, Yahoo fundamentals, ...).

See [docs/DEMO.md](docs/DEMO.md) for a live end-to-end walkthrough.

> **Naming:** the repository is `agentic-equity-research-platform`; the installable distribution is `agentic-financial-intelligence` and the import package is `financial_intelligence`. The package name is kept stable for compatibility.

Project history (phase reports, audits and remediation records) lives in [`docs/reports/`](docs/reports/).

## Live NSE/BSE quotes (optional)

`INDIA_LIVE_QUOTES_ENABLED` (unset = follows `MARKET_DATA_LIVE_ENABLED`) overlays the latest NSE quote
(public quote JSON, cookie-primed, browser-like headers) on Indian listings,
falling back to BSE and then to the Yahoo chart / fixture series. Requests are
limited to `www.nseindia.com` and `api.bseindia.com`, spaced by
`INDIA_QUOTES_MIN_INTERVAL_SECONDS` (default 1.0) and cached for
`INDIA_QUOTE_CACHE_TTL_SECONDS` (default 15). The exchanges' public endpoints
are unofficial and may change or block automated clients; check their terms
before relying on them.

## Live data feeds (optional, all default off)

| Flag | Source | Fallback |
| --- | --- | --- |
| `INDIA_FILINGS_LIVE_ENABLED` | NSE + BSE corporate announcements | sample events |
| `LIVE_NEWS_ENABLED` | GDELT DOC API, Google News RSS, `NEWS_IR_RSS_FEEDS` | sample events |
| `LIVE_REGULATORY_ENABLED` | SEC litigation/admin RSS, SEBI RSS (enforcement) | sample package |

Live items keep their provenance (`data_origin=live`, provider, URL, tier);
news is never classified as fact. These public endpoints are unofficial or
best-effort and may change, rate-limit or block clients.

## Persistence and reports

State is in memory by default. To keep workflows, checkpoints, watchlists,
research memory, API keys (stored as SHA-256 hashes) and rendered reports
across restarts, use the stdlib SQLite backend:

```bash
STORAGE_BACKEND=sqlite SQLITE_PATH=data/financial_intelligence.sqlite3 \
  uvicorn financial_intelligence.main:app
```

`POST /research/workflows/{id}/report` renders a completed workflow as
Markdown (default) or JSON (`?format=json`) and stores it in the configured
report store; `?format=deferred` keeps the legacy "report pending" response.
Rendered reports are research summaries, not investment advice.

## Status

Production-oriented, evidence-first equity research infrastructure for publicly listed companies in India and the United States.

> **Current status:** Phases 0–10 are complete and the `v1.0.0` release has been published. Runtime/package/OpenAPI metadata and the local Compose image use `1.0.0`; the Git tag `v1.0.0` and GitHub Release exist. Versioned REST, selected MCP, deterministic evaluations, bounded local reliability/load, threat/operations, rollback, and local deployment evidence pass. Exact-candidate SBOM/vulnerability evidence is retained in `release_evidence/v1.0.0/`. The owner has reviewed and accepted residual risk for the 26 candidate-affecting Critical/High findings (0 true release-blocking security findings after reachability review). Production auth/rate decisions remain deployment-dependent. Phase 11 is locked and undefined.

## Vision

The implemented platform turns supported research requests into traceable, bounded research runs. It resolves companies, plans and executes specialist work, preserves evidence and contradictions, verifies supplied typed claims, synthesizes qualified findings, and produces deterministic JSON, Markdown, and minimal Microsoft Word reports. Conversational follow-up and evaluated multilingual narrative remain deferred.

## Why this project exists

Most stock chat experiences optimize for fluent answers. This platform is designed for research that can be inspected: every material claim should resolve to time-aware evidence, authoritative sources should outrank convenience sources, conflicts should remain visible, and incomplete research should stop transparently instead of becoming confident prose.

It is intended as a production-oriented AI engineering portfolio project demonstrating bounded agent orchestration, deterministic financial computation, verification/reflection, retrieval governance, operational traceability, security, and cost control—not merely prompt chaining.

## What makes it agentic and different

The implemented system resolves company identity, builds a dependency-aware Research Plan, selects only necessary specialist capabilities, executes them sequentially within explicit budgets, and preserves verification, confidence, conflict, and critic-recommendation state before synthesis. Market, financial/filing, news/events, industry/competitor, and regulatory capabilities run on opt-in live sources (see *Live vs sample data*) with labelled fixture fallbacks; industry/competitor context is still fixture-only. A dedicated Risk Intelligence capability and autonomous targeted re-research remain deferred.

Not every capability is an agent. Calculations, ticker/date/number validation, parsing, retrieval, caching, persistence, charts and report formatting remain deterministic software. Models interpret evidence; they do not become the evidence.

## Target markets

- **India:** NSE and BSE companies, with NSE/BSE/SEBI and company Investor Relations as authoritative-source priorities.
- **United States:** NASDAQ and NYSE companies, with SEC EDGAR and company Investor Relations as authoritative-source priorities.

Company Identity is provider-neutral and accounts for exchange, country, currency, share class, aliases and listing history; ticker alone is not treated as globally unique.

## Evidence and reliability loop

```text
request -> company resolution -> research plan -> bounded tasks
        -> sources and evidence -> verification -> critic recommendations
        -> qualified findings -> synthesis -> JSON/Markdown/minimal DOCX
```

Each substantial execution will use a globally unique Research Run ID so plans, tasks, sources, evidence, model/tool calls, verification, conflicts, metrics, findings and artifacts remain traceable. Confidence is a transparent quality/coverage signal, not truth or investment advice.

The platform is designed around these constraints:

- evidence is authoritative; an LLM is never the source of truth;
- development targets zero external API and LLM cost;
- OpenRouter model selection is configuration-driven and restricted to free models;
- deterministic software performs calculations, validation, storage, and formatting;
- India (NSE/BSE) and the United States (NASDAQ/NYSE) are first-class markets;
- research remains separate from trade execution;
- REST is the primary integration surface; selected MCP is an in-process read-only/offline facade;
- the service remains independently deployable from JARVIS or any trading system.

## Free-model and token-efficiency strategy

OpenRouter is the planned model gateway, with `ALLOW_PAID_MODELS=false`, configuration-driven free model IDs, bounded retry/free fallback and no paid escalation. Large documents will be validated, parsed, normalized, deduplicated and retrieved before only relevant context is sent for reasoning. Even free-model token/latency/cache/cost telemetry remains observable.

## Planner mode configuration

The research planner operates in two modes, selected at application startup via the `PLANNER_MODE` environment variable:

| Mode | Value | Description |
|------|-------|-------------|
| Deterministic | `deterministic` | Uses `DeterministicPlannerAdapter` (default) |
| LLM | `llm` | Uses `LlmPlannerAdapter` (opt-in) |

Default: `PLANNER_MODE=deterministic`

LLM mode is fail-closed and requires **all** of the following:

1. `OPENROUTER_LIVE_ENABLED=true`
2. `PRIMARY_FREE_MODEL` is non-empty
3. `OPENROUTER_API_KEY` is configured
4. `ALLOW_PAID_MODELS=false`

Key behaviors:

- There is **no silent fallback** from LLM mode to deterministic mode.
- Planner mode is selected at application composition/startup.
- Planner mode is **NOT** a per-request API parameter.
- The production workflow has exactly one planning boundary through `PlannerPort`.
- Only zero-cost/free-model policy is supported.

## Planned delivery and interoperability

REST is the primary programmatic interface. The selected MCP facade exposes exactly two in-process read-only/offline capabilities. Streamlit and optional JARVIS interoperability remain future work. Deterministic JSON, Markdown, and minimal in-memory/base64 `.docx` artifacts are implemented; advanced templates, charts, artifact persistence, and evaluated narrative translation remain deferred. Language preferences are separated from canonical facts so later presentation work cannot change numbers, currencies, dates, company identity, or citations.

The platform supplies structured intelligence only. Broker/MT5 orders, trading credentials, position management and trading risk controls belong to a separate trading system.

## Implemented versus planned

| Implemented in the v1.0.0 release candidate | Deferred/limited after the completed Phase 0–10 roadmap |
|---|---|
| Governance, Master Architecture, ADRs, phase gates and source/evidence/model/security contracts | Broader live market/filing/qualitative providers and research coverage |
| Typed settings, fail-closed production host/header/body/correlation policy, safe telemetry, legacy REST plus selected `/v1` aliases, and minimal read-only/offline MCP facade | Owner disposition of exact-candidate Critical/High findings remains the separate v1.0.0 supply-chain release blocker; target-deployment auth/rate decisions remain deferred |
| Company resolution; market, financial, news/event, industry, and regulatory foundations; deterministic research planning/execution and governed workflows | Broader live provider coverage and production-grade durable persistence |
| Structured in-memory research history/watchlists; deterministic verification/synthesis/reporting; bounded offline production evaluations; local reliability/load, threat, SLO/runbook, deployment, and rollback evidence | Follow-up conversation, evaluated English/Telugu narrative rendering, charts, Streamlit, advanced report templates, and durable artifact storage |

No unimplemented runtime capability is represented as working today. See `PROJECT_STATUS.md` for the authoritative gate.

## Architecture reference

The supplied master diagram is preserved at [docs/architecture/agentic-financial-intelligence-platform.png](docs/architecture/agentic-financial-intelligence-platform.png). It is a **target-state concept**, not a statement of currently implemented functionality. [ARCHITECTURE.md](ARCHITECTURE.md) defines the authoritative written boundaries and phase ownership.

## Repository map

```text
src/financial_intelligence/   Python package (api, application, domain, infrastructure, ...)
tests/                        Unit, integration, contract, and evaluation tests
docs/                         Architecture, decisions, reports, and development notes
scripts/                      Future bounded development/operations scripts
data/                         Local development data placeholder; content ignored
reports/                      Generated artifact placeholder; content ignored
```

Implemented boundaries are `api`, `application`, `composition`, `config`, `domain`, `infrastructure`, `observability`, and `security`. Orchestration, providers, evidence, memory, verification, and reporting live inside `application/`, `domain/`, and `infrastructure/`; the former empty placeholder folders (`agents/`, `orchestration/`, `providers/`, `evidence/`, `memory/`, `verification/`, `reporting/`) were removed so the tree reflects only real code.

## Phase-gated development

Work proceeded through approved, test-gated phases; see the [phase history](docs/reports/PHASE_HISTORY.md). A phase may begin only after the preceding phase satisfies its tests and acceptance criteria and the project owner explicitly authorizes the next prompt/phase.

For ordered continuation context and prompt-level evidence, see [PHASE_HISTORY.md](docs/reports/PHASE_HISTORY.md). `PROJECT_STATUS.md` remains authoritative for the current gate.

## Development baseline

- Python 3.12 or 3.13
- UTF-8 source and documentation
- `src` package layout
- pytest test layout
- Phase 1 runtime dependencies are limited to FastAPI, Pydantic, pydantic-settings, and Uvicorn
- Phase 2 Prompt 1 adds no new runtime dependencies (stdlib fuzzy matching only)

Create a local environment file from `.env.example`; never commit `.env` or real credentials.

```powershell
Copy-Item .env.example .env
python -m pip install -e ".[dev]"
python -m pytest
python -m uvicorn financial_intelligence.main:app --reload
```

Health checks:

- `GET /health` — process liveness
- `GET /ready` — foundation readiness
- `GET /version` — service metadata
- `GET /companies/resolve?q=...` — local deterministic company resolution (Phase 2 Prompt 1)
- `GET /market/snapshot?q=...` — market observations and deterministic metrics (Phase 3)
- `GET /financials/snapshot?q=...` — financial fundamentals and deterministic metrics (Phase 4)
- `GET /news/events/snapshot?q=...` — news/event snapshot with evidence refs and conflicts (Phase 5; fixture-first)
- `GET /industry/context/snapshot?q=...` — industry classification + competitor relationships (Phase 5; fixture-first)
- `GET /regulatory/events/snapshot?q=...` — regulatory events with authority/allegation labels (Phase 5; fixture-first)
- `POST /research/plans` — create deterministic research plan (Phase 6; does not execute)
- `POST /research/execute` — create-and-execute a bounded plan synchronously (Phase 6; plans not persisted)
- `POST /research/synthesis` — verify supplied typed claim evidence and return deterministic evidence-linked synthesis, optionally with an in-memory structured JSON, safe Markdown, or base64 DOCX report (Phase 9 Prompts 1–3; no LLM, translation, report-side fetching, or file writes)
- `POST /research/workflows` — create a persistent research workflow (Phase 7; in-memory store)
- `GET /research/workflows` — bounded dashboard listing (`limit`/`offset`/`status_filter`/`company_id`)
- `GET /research/workflows/{workflow_id}` — load workflow status/result
- `POST /research/workflows/{workflow_id}/execute|pause|resume|cancel|approval` — govern execution
- `GET /research/workflows/{workflow_id}/memory` — structured research-memory records
- `POST /research/workflows/{workflow_id}/report?format=markdown|json` — render and store the workflow report (key figures, news, regulatory items, cited analyst summary); `format=deferred` keeps the legacy contract-only response
- `GET /health/providers` — live data-source health
- `POST /watchlists`, `GET /watchlists/{id}`, `POST /watchlists/{id}/checks` — watchlist + explicit monitoring check

Phase 10 Prompt 1 production settings:

- `ALLOWED_HOSTS` — comma-separated production host allowlist; wildcard, empty, malformed values, and production `DEBUG` logging fail closed.
- `API_MAX_REQUEST_BODY_BYTES` — whole-request limit from 4 KiB through 10 MiB; default 1 MiB. Declared and chunked oversized bodies receive a safe `413` response.
- Production request logs contain correlation ID, route template, method, status category/code, and duration only. Bodies, query values, exception messages, stack traces, credentials, and secrets are not emitted.
- API-key authentication is implemented (`AUTH_ENABLED`, `API_KEYS`, Bearer tokens); request-rate limiting is still deferred.

Phase 10 Prompt 2 hardening rejects duplicate/ambiguous Host, Content-Length, and correlation headers; malformed host ports; excessive chunk counts; secret-bearing HTTP exception details; and unbounded watchlist/memory collection requests. Prompt 3 freezes these contracts and additionally fails closed on Host outer whitespace/control, extreme numeric Host/Content-Length values, and whitespace/control correlation IDs. Prompt 3A added the approved `/v1` aliases, a static two-capability MCP facade, offline evaluations, bounded local reliability/load evidence, threat/operations guidance, and local deployment/rollback evidence. Prompt 3C historically closed the Phase 10 supply-chain gate with local SBOM/Trivy evidence. Fresh exact-candidate evidence is retained under `release_evidence/v1.0.0/`; owner disposition of its 26 candidate-affecting Critical/High findings has been completed and documented in `release_evidence/v1.0.0/OWNER_RISK_ACCEPTANCE.md`.

See [CONTRIBUTING.md](CONTRIBUTING.md) before changing the repository.

## License

Licensed under the MIT License. See [LICENSE](LICENSE).
