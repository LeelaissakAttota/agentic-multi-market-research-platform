"""Shared helpers for optional live public-data adapters (stdlib only).

* ``HostRateLimiter`` spaces requests per host.
* ``assert_allowlisted`` refuses any URL outside an adapter's host allowlist.
* ``TtlCache`` is a tiny bounded in-process cache.
* ``parse_feed`` parses RSS 2.0 / Atom safely: documents with a DOCTYPE or
  entity declarations are rejected outright (no entity expansion), sizes are
  bounded by the caller's ``BoundedHttpClient``, and item counts are capped.
"""

from __future__ import annotations

import html
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import urlsplit
from xml.etree import ElementTree

from amrp.infrastructure.http.bounded_client import (
    HttpFailureKind,
    HttpTransportError,
)


class HostRateLimiter:
    """Enforce a minimum interval between requests to the same host."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval_seconds < 0:
            msg = "min_interval_seconds must be non-negative"
            raise ValueError(msg)
        self._interval = min_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._lock = Lock()

    def acquire(self, host: str) -> None:
        with self._lock:
            now = self._monotonic()
            previous = self._last.get(host)
            if previous is not None:
                wait = self._interval - (now - previous)
                if wait > 0:
                    self._sleep(wait)
                    now = self._monotonic()
            self._last[host] = now


def assert_allowlisted(url: str, hosts: frozenset[str]) -> str:
    """Return the URL host if it is https and allowlisted, else raise."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or host not in hosts:
        msg = f"refusing non-allowlisted URL host: {host}"
        raise HttpTransportError(HttpFailureKind.INVALID_RESPONSE, msg)
    return host


class TtlCache[K, V]:
    """Bounded LRU cache with per-entry expiry."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_entries: int = 256,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0 or max_entries < 1:
            msg = "ttl_seconds and max_entries must be positive"
            raise ValueError(msg)
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock or (lambda: datetime.now(UTC))
        self._data: OrderedDict[K, tuple[datetime, V]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: K) -> tuple[bool, V | None]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False, None
            stored_at, value = entry
            if (self._clock() - stored_at).total_seconds() >= self._ttl:
                del self._data[key]
                return False, None
            self._data.move_to_end(key)
            return True, value

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str, *, limit: int = 2000) -> str:
    """Remove tags, unescape entities, collapse whitespace and control chars."""

    cleaned = html.unescape(_TAG_RE.sub(" ", text))
    cleaned = "".join(ch if ord(ch) >= 32 else " " for ch in cleaned)
    return _WS_RE.sub(" ", cleaned).strip()[:limit]


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One normalized RSS/Atom entry."""

    title: str
    link: str | None
    published: datetime | None
    summary: str
    source: str | None = None


def parse_feed_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ElementTree.Element, *names: str) -> str | None:
    for child in element:
        if _local(child.tag) in names:
            if _local(child.tag) == "link" and child.get("href"):
                return child.get("href")
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return None


def parse_feed(body: bytes, *, max_items: int = 50) -> list[FeedItem]:
    """Parse RSS 2.0 or Atom bytes into ``FeedItem`` values.

    Raises ``ValueError`` for malformed documents or any DTD/entity declaration.
    """

    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        msg = "feeds with DOCTYPE/ENTITY declarations are rejected"
        raise ValueError(msg)
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        msg = "feed is not well-formed XML"
        raise ValueError(msg) from exc
    entries = [el for el in root.iter() if _local(el.tag) in {"item", "entry"}]
    items: list[FeedItem] = []
    for entry in entries[:max_items]:
        title = strip_html(_child_text(entry, "title") or "", limit=256)
        if not title:
            continue
        link = _child_text(entry, "link")
        if link and not link.startswith(("https://", "http://")):
            link = None
        items.append(
            FeedItem(
                title=title,
                link=link,
                published=parse_feed_datetime(
                    _child_text(entry, "pubdate", "published", "updated", "date")
                ),
                summary=strip_html(_child_text(entry, "description", "summary", "content") or ""),
                source=strip_html(_child_text(entry, "source") or "", limit=128) or None,
            )
        )
    return items
