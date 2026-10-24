from __future__ import annotations

import threading

from amrp.infrastructure.http import HttpResponse
from tests.unit.test_india_quotes import FakeTransport, _client, _home, _json, _nse_payload


def test_slow_ticker_does_not_block_other_tickers() -> None:
    release = threading.Event()
    slow_started = threading.Event()

    def handler(url: str, _h: dict[str, str]) -> HttpResponse:
        if url.endswith(".com/"):
            return _home()
        if "symbol=SLOW" in url and "section" not in url:
            slow_started.set()
            assert release.wait(5)
        return _json(_nse_payload())

    client = _client(FakeTransport(handler))
    client.get_quote("WARM")  # prime cookies first
    results: dict[str, object] = {}
    slow = threading.Thread(target=lambda: results.setdefault("slow", client.get_quote("SLOW")))
    slow.start()
    assert slow_started.wait(5)
    fast = threading.Thread(target=lambda: results.setdefault("fast", client.get_quote("FAST")))
    fast.start()
    fast.join(2)
    finished_while_slow_blocked = not fast.is_alive()
    release.set()
    slow.join(5)
    fast.join(5)
    assert finished_while_slow_blocked
    assert results["fast"] is not None and results["slow"] is not None
