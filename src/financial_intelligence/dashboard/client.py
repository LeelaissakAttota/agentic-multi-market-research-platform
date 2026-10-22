"""Tiny stdlib HTTP client for the AERP REST API and the control API."""

from __future__ import annotations

import json
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
