# Demo Playbook

Two ways to demo AMRP (Agentic Multi-Market Research Platform). Every response says where its data came from
(`data_origin`: `live`, `cached_live`, `fixture`), so the demo never passes a
bundled sample off as live data.

| Mode | What you need | What it shows |
|---|---|---|
| **Offline (fixture)** | nothing | Apple / Reliance from bundled, clearly labelled demo data. Deterministic, good for CI and interviews without network. |
| **Live** | internet; `SEC_USER_AGENT` with a real email; optional `OPENROUTER_API_KEY` | Live prices (Yahoo chart, NSE/BSE quotes), live US fundamentals (SEC XBRL), live India fundamentals (Yahoo timeseries), live news and regulatory feeds, and a cited LLM analyst summary from a free OpenRouter model. |

## 1. Start the API (live mode)

```bash
cp .env.example .env
# edit .env:
#   MARKET_DATA_LIVE_ENABLED=true
#   FINANCIAL_DATA_LIVE_ENABLED=true
#   LIVE_NEWS_ENABLED=true                  # optional
#   LIVE_REGULATORY_ENABLED=true            # optional
#   SEC_USER_AGENT="Your Name your.real@email.com"   # SEC rejects noreply addresses
#   OPENROUTER_LIVE_ENABLED=true            # optional, for the analyst summary
#   OPENROUTER_API_KEY=...                  # never commit this
#   PRIMARY_FREE_MODEL=auto                 # picks a working :free model automatically
#   PLANNER_MODE=llm                        # optional; deterministic planner otherwise
python -m uvicorn amrp.main:app --reload
```

Open **http://127.0.0.1:8000/docs** (Swagger UI; on by default in development,
`API_DOCS_ENABLED=false` to turn it off). In staging/production it is off unless enabled.

## 2. Two-minute walkthrough (Swagger or curl)

1. `GET /health/providers` — which live sources answered, which failed, and when.
2. `GET /companies/resolve?q=TATAMOTORS` — former tickers map to the current listing
   (`TATAMOTORS -> TMPV`, `ZOMATO -> ETERNAL`); `q=Tata Motors` returns both demerged
   companies as candidates instead of guessing.
3. `GET /market/snapshot?q=RELIANCE&exchange=NSE` — live NSE quote merged onto the
   Yahoo daily series; freshness is `fresh`/`stale` with an explicit `as_of`.
4. `GET /financials/snapshot?q=Apple` — SEC XBRL FY figures with growth, margins,
   current ratio, debt/equity and free cash flow; `q=TCS` uses the live India source.
5. Research workflow + report (below).

## 3. End-to-end research report

```bash
H='Content-Type: application/json'   # add  -H "Authorization: Bearer <key>"  if AUTH_ENABLED=true
WF=$(curl -s -X POST localhost:8000/research/workflows -H "$H" -d '{
  "q": "Apple", "exchange": "NASDAQ",
  "objective": "comprehensive_equity_research",
  "objective_text": "Is Apple'\''s growth holding up, and what are the key risks?"
}' | python -c 'import sys,json;print(json.load(sys.stdin)["workflow_id"])')

# comprehensive research always needs a human approval step (by design)
curl -s -X POST localhost:8000/research/workflows/$WF/approval -H "$H" -d '{"decision":"approved"}'
curl -s -X POST localhost:8000/research/workflows/$WF/execute
curl -s -X POST "localhost:8000/research/workflows/$WF/report?format=markdown"
```

Narrower objectives (`market_analysis`, `financial_analysis`, `news_and_events`, ...)
skip the approval step.

The Markdown report contains:

- **Analyst Summary** — labelled *LLM-generated* (with the model name) or
  *deterministic, no LLM* (LLM off or unavailable). It answers `objective_text` and cites evidence tags such as
  `[F2]` or `[N1]`; summaries without citations are discarded.
- **Key Figures** — market metrics (`M#`) and financial metrics (`F#`) with data
  origin, provider and as-of date.
- **Top News** (`N#`) and **Regulatory Items** (`R#`) with dates and source links.
- Task results per capability, evidence references and research memory.

## 4. Talking points

- Evidence first: every number carries its source, authority tier, origin and as-of date.
- Honest degradation: if a live source fails the platform falls back to a labelled
  sample (`status=degraded`, "FIXTURE/DEMO SAMPLE") instead of failing or pretending.
- Free by design: no paid APIs; LLM calls are restricted to `:free` OpenRouter models
  with automatic selection and fallback.
- Human in the loop: comprehensive research requires approval before execution.
- Quality: ruff, mypy strict and 1,400+ automated tests (CI never touches the network).

## 5. Known limitations (say them out loud)

- India fundamentals come from Yahoo's unofficial fundamentals timeseries (Tier 2
  aggregator, annual consolidated figures). If it is unreachable, only Reliance has a
  bundled sample, clearly labelled as fixture data.
- US fundamentals need a real-email `SEC_USER_AGENT`; without it SEC data stays off.
- Free LLM models are rate-limited; the first workflow after start-up can take
  10-25 s while a working free model is selected (the choice is cached on disk).
- Research context only. Not investment advice.
