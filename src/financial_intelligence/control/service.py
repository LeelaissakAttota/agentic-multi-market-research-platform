"""Wire the control runtime to ExecuteResearchPlan (reuses ApprovalStatus vocabulary)."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from financial_intelligence.application.company_resolution import CompanyQuery
from financial_intelligence.application.research_execution_contracts import ExecuteResearchPlanQuery
from financial_intelligence.application.step_gate import StepGateOutcome
from financial_intelligence.composition import AppContainer, build_container
from financial_intelligence.config.settings import Settings, get_settings
from financial_intelligence.control.runtime import (
    AuditLog,
    ControlRun,
    ControlSettings,
    RunManager,
    Step,
)
from financial_intelligence.control.secrets_store import SecretStore, default_config_dir
from financial_intelligence.dashboard.prefs import load_prefs, settings_update, uses_custom_router
from financial_intelligence.domain.identity import CountryCode, ExchangeCode, TickerSymbol
from financial_intelligence.domain.orchestration import (
    ExecutionControl,
    ResearchObjective,
    ResearchTask,
    TaskExecutionResult,
    TaskResultStatus,
)
from financial_intelligence.domain.workflow import ApprovalStatus

#: Env-style key name -> Settings field name.
KEY_FIELDS: dict[str, str] = {
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "ALPHA_VANTAGE_API_KEY": "alpha_vantage_api_key",
    "FINNHUB_API_KEY": "finnhub_api_key",
}

#: Other LLM providers' keys: stored write-only/masked for the dashboard Settings page.
#: The research backend still calls only OpenRouter (free models); see MODEL_POLICY.md.
EXTRA_PROVIDER_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "CUSTOM_LLM_API_KEY",
)

_OK = (TaskResultStatus.SUCCESS, TaskResultStatus.PARTIAL)


class ControlStepGate:
    """Synchronous StepGatePort that blocks on the run's human approval gate."""

    def __init__(self, run: ControlRun, control: ExecutionControl) -> None:
        self._run = run
        self._control = control

    def before(self, task: ResearchTask) -> StepGateOutcome:
        step, decision = self._run.gate(
            task.task_type.value,
            task.description,
            {
                "description": task.description,
                "capability_id": task.capability_id,
                "task_id": task.task_id.as_text(),
            },
        )
        if decision.stopped:
            return StepGateOutcome(ApprovalStatus.REJECTED, task, stopped=True, handle=step)
        if not decision.approved:
            return StepGateOutcome(ApprovalStatus.REJECTED, task, handle=step)
        description = decision.inputs.get("description")
        if isinstance(description, str) and description.strip() and description != task.description:
            task = replace(task, description=description)
        self._run.step_started(step)
        status = (
            ApprovalStatus.NOT_REQUIRED
            if step.decision and step.decision.value == "not_required"
            else ApprovalStatus.APPROVED
        )
        return StepGateOutcome(status, task, handle=step)

    def after(self, outcome: StepGateOutcome, result: TaskExecutionResult) -> None:
        if isinstance(outcome.handle, Step):
            ok = result.status in _OK
            self._run.step_finished(
                outcome.handle,
                ok=ok,
                output={
                    "status": result.status.value,
                    "summary": result.output_summary or result.message,
                    "evidence_refs": len(result.evidence_refs),
                },
                error=None
                if ok
                else f"{result.error_code or result.status.value}: {result.message}",
            )


@dataclass
class _RunContext:
    container: AppContainer
    company: Any
    company_query: CompanyQuery
    tasks: dict[str, ResearchTask]


def company_query_from_params(params: dict[str, Any]) -> CompanyQuery:
    def text(key: str) -> str | None:
        value = params.get(key)
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    country, exchange, ticker = text("country"), text("exchange"), text("ticker")
    return CompanyQuery(
        raw_query=text("q") or "",
        country=CountryCode(country) if country else None,
        exchange=ExchangeCode(exchange) if exchange else None,
        ticker=TickerSymbol(ticker) if ticker else None,
    )


class EquityResearchControl:
    def __init__(
        self, base_settings: Settings | None = None, config_dir: Path | None = None
    ) -> None:
        self.config_dir = config_dir or default_config_dir("equity-research")
        self.base_settings = base_settings or get_settings()
        self.secrets = SecretStore(
            self.config_dir / "secrets.json", tuple(KEY_FIELDS) + EXTRA_PROVIDER_KEYS
        )
        self.prefs_file = self.config_dir / "dashboard_settings.json"
        self._contexts: dict[str, _RunContext] = {}
        self._lock = threading.Lock()
        self.manager = RunManager(
            self._worker,
            self._rerun,
            ControlSettings(self.config_dir / "control_settings.json"),
            AuditLog(self.config_dir / "audit.jsonl"),
        )

    def settings_with_keys(self) -> Settings:
        """Fresh settings per run; keys re-read from the secrets file (no restart)."""
        update: dict[str, Any] = {}
        for name, field_name in KEY_FIELDS.items():
            value = self.secrets.get(name)
            if value:
                update[field_name] = SecretStr(value)
        prefs = load_prefs(self.prefs_file) if self.prefs_file.exists() else {}
        if prefs:
            # Dashboard Settings (live toggles, SEC contact, model choice) apply to
            # the next run without a restart.
            update.update(settings_update(prefs))
            custom = self.secrets.get("CUSTOM_LLM_API_KEY")
            if uses_custom_router(prefs) and custom:
                update["openrouter_api_key"] = SecretStr(custom)
        return self.base_settings.model_copy(update=update)

    def analyze_market(self, body: dict[str, Any]) -> dict[str, Any]:
        """Evidence-tagged market analysis with the configured (free-first) model."""

        from financial_intelligence.control.analysis import analyze

        settings = self.settings_with_keys()
        if not settings.openrouter_live_enabled:
            return {
                "status": "not_available",
                "reason": "OPENROUTER_LIVE is off (Settings > Live data)",
            }
        router = build_container(settings).llm_router
        result = analyze(
            router,
            str(body.get("market") or ""),
            str(body.get("instrument") or ""),
            list(body.get("evidence") or []),
        )
        self.manager.audit.record(
            {
                "event": "market_analysis",
                "instrument": str(body.get("instrument"))[:32],
                "status": result.get("status"),
                "model": result.get("model"),
            }
        )
        return result

    def _worker(self, run: ControlRun) -> dict[str, Any]:
        objective = ResearchObjective(str(run.params.get("objective") or "company_overview"))
        query = ExecuteResearchPlanQuery(
            company_query=company_query_from_params(run.params), objective=objective
        )
        container = build_container(self.settings_with_keys())
        control = ExecutionControl()
        gate = ControlStepGate(run, control)

        plan_result = container.create_research_plan.execute(query.as_plan_query())
        if (
            plan_result.plan is None
            or plan_result.resolution is None
            or plan_result.resolution.company is None
        ):
            raise ValueError(f"plan unavailable: {plan_result.status.value}: {plan_result.message}")
        company = plan_result.resolution.company
        run.log(
            f"plan {plan_result.plan.plan_id.as_text()} for {company.company_id.as_text()} "
            f"({len(plan_result.plan.tasks)} tasks, objective={objective.value})"
        )
        with self._lock:
            self._contexts[run.run_id] = _RunContext(
                container,
                company,
                query.company_query,
                {t.task_id.as_text(): t for t in plan_result.plan.tasks},
            )
        result = container.execute_research_plan.execute_prepared(
            plan_result.plan,
            company=company,
            company_query=query.company_query,
            resolution=plan_result.resolution,
            query=query,
            control=control,
            step_gate=gate,
        )
        data = result.to_dict()
        run.log(f"execution status: {data.get('status')}")
        return data

    def _rerun(
        self, run: ControlRun, step: Step, inputs: dict[str, Any]
    ) -> tuple[bool, Any, str | None]:
        with self._lock:
            ctx = self._contexts.get(run.run_id)
        task_id = str(inputs.get("task_id") or "")
        if ctx is None or task_id not in ctx.tasks:
            return False, None, "run context unavailable for re-run"
        task = ctx.tasks[task_id]
        description = inputs.get("description")
        if isinstance(description, str) and description.strip():
            task = replace(task, description=description.strip())
        result = ctx.container.capability_executor.execute_task(
            task, company=ctx.company, company_query=ctx.company_query
        )
        ok = result.status in _OK
        return (
            ok,
            {"status": result.status.value, "summary": result.output_summary or result.message},
            (None if ok else result.message),
        )


def admin_token_from_env() -> str:
    token = os.environ.get("EQUITY_RESEARCH_CONTROL_TOKEN", "")
    if not token:
        raise SystemExit("EQUITY_RESEARCH_CONTROL_TOKEN must be set (>=16 chars)")
    return token
