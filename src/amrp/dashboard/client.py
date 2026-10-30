"""Tiny stdlib HTTP client for the AMRP REST API and the control API."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


@dataclass
class JsonClient:
    base_url: str
    token: str = ""
    timeout: float = 180.0
    operator: str = "dashboard"

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = self.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json", "X-Operator": self.operator}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode() or "null"
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, _error_message(exc.read().decode(errors="replace"))) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"cannot reach {self.base_url}: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise ApiError(
                0, f"no response from {self.base_url} within {self.timeout:.0f}s ({exc})"
            ) from None
        return json.loads(raw)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, body=body, params=params or None)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body=body)


def _error_message(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if data.get("detail"):
            return str(data["detail"])
    return raw[:300]


def probe(url: str, headers: dict[str, str], timeout: float = 15.0) -> tuple[bool, str]:
    """GET url; never echo headers (they carry the key). Returns (ok, short message)."""

    req = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"connected (HTTP {resp.status})"
    except urllib.error.HTTPError as exc:
        hint = "key rejected" if exc.code in (401, 403) else "provider error"
        return False, f"{hint} (HTTP {exc.code})"
    except urllib.error.URLError as exc:
        return False, f"cannot reach provider: {exc.reason}"


def probe_json(url: str, headers: dict[str, str], timeout: float = 15.0) -> tuple[bool, str, Any]:
    """Like :func:`probe` but also returns the decoded JSON body (``None`` on failure)."""

    req = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8") or "null")
            return True, f"connected (HTTP {resp.status})", body
    except urllib.error.HTTPError as exc:
        hint = "key rejected" if exc.code in (401, 403) else "provider error"
        return False, f"{hint} (HTTP {exc.code})", None
    except (urllib.error.URLError, ValueError) as exc:
        return False, f"cannot reach provider: {getattr(exc, 'reason', exc)}", None


def resolve_company(api: JsonClient, candidates: tuple[str, ...]) -> dict[str, Any] | None:
    """First RESOLVED/AMBIGUOUS resolution among phrase candidates, else None."""

    for phrase in candidates:
        try:
            res = api.get("/companies/resolve", q=phrase)
        except ApiError:
            continue
        if isinstance(res, dict) and res.get("status") in ("RESOLVED", "AMBIGUOUS"):
            res["matched_phrase"] = phrase
            return res
    return None


def candidate_choices(resolution: dict[str, Any]) -> list[dict[str, str]]:
    """Ambiguous candidates -> [{label, q, ticker, exchange}] for pick buttons."""

    out = []
    for cand in resolution.get("candidates") or []:
        company = cand.get("company") or {}
        listings = cand.get("matched_listings") or []
        primary = next(
            (x for x in listings if x.get("is_primary")), listings[0] if listings else {}
        )
        name = str(company.get("display_name") or company.get("legal_name") or "?")
        ticker, exchange = str(primary.get("ticker") or ""), str(primary.get("exchange") or "")
        label = f"{name} ({exchange}:{ticker})" if ticker else name
        out.append({"label": label, "q": name, "ticker": ticker, "exchange": exchange})
    return out


_SUFFIX_EXCHANGE = {".NS": "NSE", ".NSE": "NSE", ".BO": "BSE", ".BSE": "BSE"}
_EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=5&newsCount=0"


def yahoo_symbol(ticker: str, exchange: str) -> str:
    """Exchange-qualified Yahoo-style symbol (RELIANCE + NSE -> RELIANCE.NS)."""

    return f"{ticker}{_EXCHANGE_SUFFIX.get(exchange.upper(), '')}" if ticker else ""


def resolved_symbol(resolution: dict[str, Any] | None) -> str:
    """Primary listing symbol of a RESOLVED company, '' if unknown."""

    company = (resolution or {}).get("company") or {}
    listings = [x for sec in company.get("securities") or [] for x in sec.get("listings") or []]
    if not listings:
        return ""
    primary = next((x for x in listings if x.get("is_primary")), listings[0])
    return yahoo_symbol(str(primary.get("ticker") or ""), str(primary.get("exchange") or ""))


def strip_suffix(text: str) -> str:
    """'RELIANCE.NS' -> 'RELIANCE'; other text unchanged."""

    t = text.strip()
    for suf in _SUFFIX_EXCHANGE:
        if t.upper().endswith(suf) and len(t) > len(suf):
            return t[: -len(suf)]
    return t


def yahoo_lookup(text: str, timeout: float = 8.0) -> list[tuple[str, str]]:
    """Free Yahoo search -> [(symbol, name)] equities, Indian listings first. [] on failure."""

    url = YAHOO_SEARCH.format(q=urllib.parse.quote(text.strip()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return []
    out = []
    for q in data.get("quotes") or []:
        if q.get("quoteType") not in (None, "EQUITY"):
            continue
        sym = str(q.get("symbol") or "")
        if sym:
            out.append((sym, str(q.get("longname") or q.get("shortname") or sym)))
    out.sort(key=lambda x: 0 if x[0].endswith((".NS", ".BO")) else 1)
    return out


def resolve_any(
    api: JsonClient,
    text: str,
    candidates: tuple[str, ...],
    lookup: Any = yahoo_lookup,
) -> dict[str, Any] | None:
    """Resolve a ticker or name with no exchange suffix needed.

    Order: parsed phrase candidates; the bare symbol (suffix stripped);
    then a Yahoo search whose symbols (bare, .NS, .BO) and names are retried.
    Adds ``resolved_symbol`` (e.g. RELIANCE.NS) when a listing is known.
    """

    tries: list[str] = list(candidates)
    bare = strip_suffix(text)
    if bare and bare not in tries:
        tries.append(bare)
    res = resolve_company(api, tuple(tries))
    if res is None:
        extra: list[str] = []
        for sym, name in lookup(bare or text) or []:
            for t in (strip_suffix(sym), name):
                if t and t not in tries and t not in extra:
                    extra.append(t)
        res = resolve_company(api, tuple(extra)) if extra else None
    if res is not None and res.get("status") == "AMBIGUOUS":
        res = prefer_indian_primary(res)
    if res is not None and res.get("status") == "RESOLVED" and not res.get("resolved_symbol"):
        res["resolved_symbol"] = resolved_symbol(res)
    return res


def _base_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    words = [w for w in n.split() if w not in {"ltd", "limited", "inc", "corp", "plc", "the"}]
    return " ".join(words)


def prefer_indian_primary(res: dict[str, Any]) -> dict[str, Any]:
    """AMBIGUOUS between listings of ONE company (e.g. Wipro NSE vs its NYSE ADR) -> NSE pick.

    Only when every candidate has the same base name and exactly one has an NSE
    listing; otherwise the ambiguity (and the choice buttons) is kept.
    """

    cands = res.get("candidates") or []
    names = {_base_name(str((c.get("company") or {}).get("display_name") or "")) for c in cands}
    if len(cands) < 2 or len(names) != 1 or "" in names:
        return res
    nse = [
        (c, x)
        for c in cands
        for x in c.get("matched_listings") or []
        if str(x.get("exchange") or "").upper() == "NSE"
    ]
    if len({id(c) for c, _ in nse}) != 1:
        return res
    cand, listing = nse[0]
    out = dict(res)
    out.update(
        status="RESOLVED",
        company=cand.get("company") or {},
        auto_picked=True,
        resolved_symbol=yahoo_symbol(str(listing.get("ticker") or ""), "NSE"),
        pick_exchange="NSE",
        pick_ticker=str(listing.get("ticker") or ""),
    )
    return out
