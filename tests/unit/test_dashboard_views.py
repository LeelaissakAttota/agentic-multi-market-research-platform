"""Unit tests for the v2 dashboard view helpers (pure functions, no Streamlit)."""

from __future__ import annotations

from pathlib import Path

from amrp.dashboard import views as v


def _wf(wid: str, status: str = "completed", approval: str = "not_required", **kw: object):
    return {
        "workflow_id": wid,
        "status": status,
        "approval_status": approval,
        "objective": kw.get("objective", "financial_analysis"),
        "created_at": kw.get("created_at", "2026-10-22T18:59:30Z"),
        "company_query": {"raw_query": kw.get("company", "Apple")},
    }


def test_workflow_stats_counts_real_states_only() -> None:
    wfs = [_wf("a"), _wf("b", "partial"), _wf("c", "created", "pending"), _wf("d", "failed")]
    assert v.workflow_stats(wfs) == v.WorkflowStats(4, 2, 1, 1)
    assert v.workflow_stats([]) == v.WorkflowStats(0, 0, 0, 0)


def test_workflow_rows_sorted_newest_first_with_labels() -> None:
    rows = v.workflow_rows(
        [_wf("a", created_at="2026-01-01T00:00:00Z"), _wf("b", "created", "pending", company="TCS")]
    )
    assert rows[0]["Company"] == "TCS"
    assert rows[0]["Status"] == "Pending approval"
    assert rows[1]["Objective"] == "Financial Analysis"
    assert rows[1]["Created"] == "2026-01-01 00:00 UTC"


def test_split_approvals_and_step_tracker() -> None:
    groups = v.split_approvals([_wf("a", approval="approved"), _wf("b", "created", "pending")])
    assert [w["workflow_id"] for w in groups["pending"]] == ["b"]
    assert len(groups["approved"]) == 1 and groups["rejected"] == []
    assert v.step_states(None)[0] == ("Plan", "todo")
    assert v.step_states(_wf("a"))[-1] == ("Complete", "done")
    assert v.step_states(_wf("b", "created", "pending"))[0] == ("Plan", "current")
    assert ("Research", "failed") in v.step_states(_wf("c", "failed"))


def test_figures_series_and_missing_values_stay_none() -> None:
    rows = [{"label": "Last close", "value": "USD 199.00"}, {"label": "Empty", "value": ""}]
    assert v.figure_value(rows, "last close") == "USD 199.00"
    assert v.figure_value(rows, "Empty") is None
    assert v.figure_value(rows, "Missing") is None
    snap = {
        "data_origin": "fixture",
        "series": {
            "currency": "USD",
            "data_origin": "fixture",
            "bars": [
                {"session_date": "2026-08-06", "close": "197.5"},
                {"session_date": "2026-08-07", "close": "199.00"},
                {"session_date": "bad"},
            ],
        },
    }
    assert v.close_series(snap) == [("2026-08-06", 197.5), ("2026-08-07", 199.0)]
    assert v.latest_close(snap) == (199.0, "USD", "fixture")
    assert v.latest_close(None) == (None, None, None)


def test_evidence_refs_dedupes_sources() -> None:
    ref = {"source_id": "63333333-aaaa", "data_origin": "live", "authority_tier": 1}
    report = {
        "research_memory": [{"capability": "financial_intelligence", "evidence_refs": [ref, ref]}]
    }
    out = v.evidence_refs(report)
    assert len(out) == 1 and out[0]["Origin"] == "live" and out[0]["Source id"] == "63333333"


def test_filter_and_csv_export() -> None:
    rows = v.workflow_rows([_wf("a"), _wf("b", company="TCS", objective="news_and_events")])
    assert len(v.filter_reports(rows, company="TCS")) == 1
    assert len(v.filter_reports(rows, objective="Financial Analysis")) == 1
    assert v.filter_reports(rows, text="nothing-matches") == []
    csv_text = v.to_csv(rows)
    assert csv_text.splitlines()[0] == "Company,Objective,Status,Created,Missing"
    assert "workflow_id" not in csv_text
    assert v.to_csv([]) == ""


def test_watchlist_local_store(tmp_path: Path) -> None:
    p = tmp_path / "wl.json"
    assert v.load_watchlist(p) == []
    v.add_to_watchlist("Reliance", p)
    v.add_to_watchlist("reliance", p)
    v.add_to_watchlist("TCS", p)
    assert v.load_watchlist(p) == ["Reliance", "TCS"]
    assert v.remove_from_watchlist("RELIANCE", p) == ["TCS"]
    p.write_text("{broken", encoding="utf-8")
    assert v.load_watchlist(p) == []


def test_history_merge_prefers_api(tmp_path: Path) -> None:
    p = tmp_path / "h.json"
    v.record_history({"workflow_id": "a", "company": "Apple", "status": "completed"}, p)
    v.record_history({"workflow_id": "a", "company": "Apple", "status": "partial"}, p)
    v.record_history({"workflow_id": "old", "company": "TCS", "status": "completed"}, p)
    hist = v.load_history(p)
    assert [h["workflow_id"] for h in hist] == ["a", "old"]
    merged = v.merge_workflows([_wf("a", "failed")], v.history_as_workflows(hist))
    by_id = {w["workflow_id"]: w for w in merged}
    assert by_id["a"]["status"] == "failed"
    assert by_id["old"]["company_query"]["raw_query"] == "TCS"


def test_provider_cards_never_invent_latency() -> None:
    payload = {
        "providers": [
            {
                "provider": "yahoo",
                "state": "ok",
                "successes": 3,
                "failures": 0,
                "last_success_at": "2026-10-22T18:00:00Z",
                "last_failure": None,
            },
            {"state": "ok"},
        ]
    }
    cards = v.provider_cards(payload)
    assert len(cards) == 1
    assert cards[0]["latency_ms"] is None
    assert cards[0]["successes"] == 3.0
    assert cards[0]["last_success"] == "2026-10-22 18:00 UTC"
    assert v.provider_cards(None) == []


def test_revenue_profit_series_needs_two_periods_each() -> None:
    from amrp.dashboard.views import revenue_profit_series

    rows = [
        {"label": "Revenue", "value": "USD 383.3 B", "detail": "FY2023"},
        {"label": "Revenue", "value": "USD 391.0 B", "detail": "FY2024"},
        {"label": "Net income", "value": "USD 97.0 B", "detail": "FY2023"},
        {"label": "Net income", "value": "USD 93.7 B", "detail": "FY2024"},
        {"label": "Revenue growth", "value": "2.0%", "detail": "FY2024"},
    ]
    out = revenue_profit_series(rows)
    assert ("FY2024", "Revenue", 391.0e9) in out
    assert len(out) == 4
    assert revenue_profit_series(rows[:3]) == []
    assert revenue_profit_series([]) == []
