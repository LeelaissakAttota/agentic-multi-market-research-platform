# Data Source Strategy

## Purpose

Source selection is a governed domain policy, not an incidental HTTP detail. Provider adapters must be replaceable, optional where possible, and explicit about authority, terms, credentials, quotas, geographic coverage, delay, and freshness.

Phase 0 recorded candidates; the adapters actually implemented are listed under *Implemented live sources*. Listing a source does not certify its availability, licensing, completeness, or fitness.

## Authority hierarchy

1. **Tier 1 — Authoritative:** SEC EDGAR; NSE; BSE; SEBI; official government/regulator sources; official company investor-relations sites and filings.
2. **Tier 2 — Structured financial data:** free market-data APIs, exchange-derived data, and reputable datasets whose use and freshness are understood.
3. **Tier 3 — Reputable news/business sources:** established financial/business news feeds or APIs used lawfully and with attribution.
4. **Tier 4 — General web:** cautious supplemental discovery; it cannot silently override authoritative filings.

Authority is claim-type aware. An exchange price, audited financial statement, regulator action, and management outlook may each have different best sources. Conflicts are stored rather than collapsed.

Missing authoritative evidence is itself a quality signal: it reduces claim confidence/coverage and must be disclosed rather than replaced silently by a lower tier.

## Initial market strategy

### India

Candidate authoritative/public sources include NSE, BSE, SEBI, official company investor-relations sites, annual reports, quarterly results, and corporate announcements.

### United States

Candidate authoritative/public sources include SEC EDGAR, official company investor-relations sites, annual reports, Forms 10-K/10-Q/8-K, earnings releases, and exchange/company reference information.

### Optional market-data adapters

Optional adapters never become mandatory; the platform stays useful (fixture-labelled) when any of them is disabled or failing. Their unofficial endpoints may change or rate-limit; review each provider's terms before production use.

## Implemented live sources (all opt-in)

| Domain | Source | Tier | Notes |
|---|---|---|---|
| Market history | Yahoo Finance chart API | 2 | no key; newest 120 sessions |
| Market history (backup) | Alpha Vantage free tier | 2 | optional key, persisted 25/day budget |
| Indian live quotes | NSE quote JSON, BSE quote API | 1 | cookie-primed, rate-spaced, cached |
| US fundamentals | SEC EDGAR XBRL companyfacts | 1 | needs `SEC_USER_AGENT` with a real email |
| India fundamentals | Yahoo fundamentals timeseries | 2 | no key; unofficial aggregator; annual consolidated |
| Company universe | BSE active equity list, SEC company_tickers_exchange | 1 | disk-cached daily |
| News | GDELT DOC API, Google News RSS, company IR RSS | 3 | news is never classified as fact |
| India announcements | NSE + BSE corporate announcements | 1 | exchange disclosures as events |
| Regulatory | SEC litigation/admin RSS, SEBI RSS | 1 | allegation vs final action labelled |
| LLM | OpenRouter `:free` models only | — | `ALLOW_PAID_MODELS=false` fail-closed |

Not yet live: industry/competitor context (fixture only). Finnhub is not integrated.

## Provider port expectations

Every adapter should declare:

- capability, market, exchange, and identifier coverage;
- authority level and supported claim types;
- authentication and optional/required status;
- rate/quota behavior and bounded backoff rules;
- timestamps, time zone, delay, and freshness semantics;
- currency, unit, period, and corporate-action semantics;
- licensing/terms and attribution requirements;
- response-size/content-type validation and parsing constraints;
- deterministic error categories and health signals.

Adapters return normalized data plus provenance; they do not decide final truth, synthesize prose, or leak vendor types into the domain.

## Acquisition rules

- Identify the product and provide an appropriate contact/user agent where a source requires it.
- Respect robots directives, terms, rate limits, copyright, and redistribution restrictions.
- Use connection/read/write/pool timeouts and bounded retries only for retryable failures.
- Validate HTTPS and external URLs against SSRF policy before requests.
- Limit redirects, response bytes, document bytes, decompression, and parse work.
- Hash and timestamp accepted raw content; preserve retrieval and source publication dates separately.
- Cache according to source rules and information volatility; cache failures only briefly and explicitly.
- Distinguish “no result,” “unsupported,” “rate limited,” “temporarily unavailable,” and “invalid response.”

## Conflict and fallback behavior

Fallback improves availability, not authority. A lower-authority value cannot silently replace a conflicting authoritative value. Store both observations, their as-of/period context, and a contradiction record. If the conflict cannot be resolved, output uncertainty and cite both.

## Freshness

Freshness is contextual and configurable by claim type. Market quotes, corporate announcements, annual statements, and regulator rules have different lifetimes. Store `source_date`, `retrieved_at`, reporting period, and evaluated freshness status; never infer “latest” solely from retrieval time.

## Source onboarding gate

Before enabling a provider: document purpose and authority; review current terms and cost; prove optionality; define schemas and errors; add fixtures and contract tests; add rate/timeout/size controls; map provenance; test secrets/logging; and record operational ownership.
