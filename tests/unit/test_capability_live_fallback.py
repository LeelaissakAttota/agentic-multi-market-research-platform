"""Live fallback for industry/regulatory agents when fixtures have nothing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from amrp.domain.data_origin import DataOrigin
from amrp.domain.orchestration import TaskExecutionResult, TaskResultStatus
from amrp.infrastructure.orchestration.capability_executor import Phase6CapabilityExecutor


def _executor(live):
    m = MagicMock()
    return Phase6CapabilityExecutor(
        resolve_company=m,
        get_market_snapshot=m,
        get_financial_snapshot=m,
        get_news_event_snapshot=m,
        get_industry_snapshot=m,
        get_regulatory_snapshot=m,
        live_context=live,
    )


def _ctx():
    listing = SimpleNamespace(
        exchange=SimpleNamespace(as_text=lambda: "NSE"),
        ticker=SimpleNamespace(as_text=lambda: "TCS"),
    )
    company = SimpleNamespace(
        company_id=MagicMock(),
        legal_name="Tata Consultancy Services",
        display_name="TCS",
        securities=[SimpleNamespace(listings=[listing])],
    )
    query = SimpleNamespace(exchange=None, ticker=None)
    task = SimpleNamespace(task_id=MagicMock())
    return task, company, query


def _unavailable(task):
    return TaskExecutionResult(
        task_id=task.task_id, status=TaskResultStatus.UNAVAILABLE, message="no fixture"
    )


def test_unavailable_industry_upgraded_from_live(monkeypatch):
    live = MagicMock()
    live.industry.return_value = {
        "status": "ok",
        "message": "3 peer(s)",
        "items": [1, 2, 3],
        "figures": [],
        "provider_name": "yahoo_finance",
    }
    ex = _executor(live)
    task, company, query = _ctx()
    out = ex._live_fallback(task, _unavailable(task), company, query, kind="industry")
    assert out.status is TaskResultStatus.SUCCESS
    assert out.evidence_refs[0].data_origin is DataOrigin.LIVE
    live.industry.assert_called_once_with(
        exchange="NSE", ticker="TCS", name="Tata Consultancy Services"
    )


def test_live_unavailable_keeps_original():
    live = MagicMock()
    live.regulatory.return_value = {"status": "unavailable", "items": []}
    ex = _executor(live)
    task, company, query = _ctx()
    orig = _unavailable(task)
    assert ex._live_fallback(task, orig, company, query, kind="regulatory") is orig


def test_live_exception_keeps_original_and_no_live_is_noop():
    live = MagicMock()
    live.regulatory.side_effect = RuntimeError("boom")
    task, company, query = _ctx()
    orig = _unavailable(task)
    assert _executor(live)._live_fallback(task, orig, company, query, kind="regulatory") is orig
    assert _executor(None)._live_fallback(task, orig, company, query, kind="industry") is orig
