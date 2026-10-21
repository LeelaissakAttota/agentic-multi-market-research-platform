# AERP (Equity Research Platform) demo data

Fictional company names in this folder (Zentrix Robotics, Zentrix Dynamics, Bluepeak Lithium Corp, Kaveri Quantum Foods,
Moonlit Robotics) are made up on purpose and **must return `NOT_FOUND`** - the platform must never invent data.
Offline runs use bundled sample (fixture) data; old sample prices show `"freshness": "stale"` - that is expected, not an error.

| Path | What it is |
|---|---|
| `requests/N01-N22_*.json` | Normal requests (expected HTTP 200) |
| `requests/E01-E06_*.json` | Error requests (400 / 422 / 404) |
| `data/companies_to_try.csv` | Real + fictional companies with expected resolve status |
| `research_questions.csv` | Research questions per objective (for the Swagger `/docs` demo) |
| `demo_watchlist.json` | Body for `POST /watchlists` |
| `send.ps1` | Send one request file and pretty-print the response |
| `run-all.ps1` | Run all requests and print a PASS/FAIL table |

## Start (PowerShell, repo root)

Offline, readable logs, auth off (local only, binds to 127.0.0.1):

```powershell
$env:AUTH_ENABLED = "false"; $env:OPENROUTER_LIVE_ENABLED = "false"; $env:LOG_FORMAT = "text"
.\.venv\Scripts\python.exe -m uvicorn financial_intelligence.main:app --host 127.0.0.1 --port 8000
```

Swagger UI: http://127.0.0.1:8000/docs

With your normal `.env` (auth on): set `$env:AERP_DEMO_API_KEY` to one of your `API_KEYS` in the
terminal that runs the scripts. The key is only sent as a Bearer header, never printed.

## Run

```powershell
cd examples\demo-data
.\run-all.ps1                 # all 28
.\run-all.ps1 -Filter "E*"    # error cases only
.\send.ps1 requests\N05_resolve_apple.json
```

## Logs

`LOG_FORMAT=text` gives one readable line per event; JSON is the default. Keys, Bearer tokens and
`apikey=` values are shown as `[REDACTED]`. 4xx errors log route, method and status; validation errors
log field names and reasons, never the submitted value; crashes log `exception_where` (file:function:line).
