"""Probe runner: plan the scenarios, send them under budget, capture the raw bytes."""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from cacherift.capabilities.registry import CapabilityRegistry, PricingRegistry
from cacherift.core.freeze import freeze_json
from cacherift.core.models import CaptureRecord, EndpointIdentity
from cacherift.core.pricing import estimate_cost
from cacherift.dialects.base import get_dialect_adapter
from cacherift.io.redact import redact_headers, scrub_secrets, secret_fragments
from cacherift.probe.budget import (
    BudgetExceededError,
    BudgetTracker,
    ProbeBudget,
    select_scenarios,
    worst_case_cost,
)
from cacherift.probe.scenarios import (
    MAX_OUTPUT_TOKENS,
    ProbeScenario,
    ProbeStep,
    build_scenarios,
)

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_ANTHROPIC_TTL_BETA = "extended-cache-ttl-2025-04-11"


class ProbeConfigError(ValueError):
    """The probe cannot run as configured; nothing has been sent."""


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"probe-{stamp}-{secrets.token_hex(3)}"


@dataclass(frozen=True, slots=True)
class PlannedStep:
    record_id: str
    scenario: str
    label: str
    intent: str
    session_id: str
    parent_record_id: str | None
    body: bytes
    estimated_tokens: int
    worst_case_usd: float | None


@dataclass(frozen=True, slots=True)
class PlannedScenario:
    name: str
    steps: tuple[PlannedStep, ...]
    worst_case_usd: float | None
    fits_budget: bool


@dataclass(frozen=True, slots=True)
class ProbePlan:
    run_id: str
    scenarios: tuple[PlannedScenario, ...]

    @property
    def steps(self) -> tuple[PlannedStep, ...]:
        return tuple(step for scenario in self.scenarios for step in scenario.steps)

    @property
    def planned_steps(self) -> tuple[PlannedStep, ...]:
        """Steps that will actually be sent: whole scenarios that fit the budget."""
        return tuple(
            step for scenario in self.scenarios if scenario.fits_budget for step in scenario.steps
        )

    @property
    def unpriced(self) -> tuple[str, ...]:
        """Scenarios whose cost cannot be bounded because the model has no pricing rule."""
        return tuple(
            scenario.name for scenario in self.scenarios if scenario.worst_case_usd is None
        )

    @property
    def skipped(self) -> tuple[str, ...]:
        """Scenarios that will not be sent: unpriced, or more than the budget can hold."""
        return tuple(scenario.name for scenario in self.scenarios if not scenario.fits_budget)

    @property
    def worst_case_usd(self) -> float | None:
        """Sum over the admitted scenarios, or None when any of them is unpriced."""
        costs = [scenario.worst_case_usd for scenario in self.scenarios if scenario.fits_budget]
        return _sum_or_none(costs)


def _sum_or_none(costs: Sequence[float | None]) -> float | None:
    """Total of costs that are all known, or None as soon as one of them is not."""
    priced = [cost for cost in costs if cost is not None]
    return sum(priced) if costs and len(priced) == len(costs) else None


@dataclass(frozen=True, slots=True)
class ProbeRunResult:
    records: tuple[CaptureRecord, ...]
    stopped_reason: str | None
    spent_usd: float


class ProbeRunner:
    """Plans and executes one probe run against a single upstream endpoint."""

    def __init__(
        self,
        identity: EndpointIdentity,
        *,
        url: str,
        api_key_env: str,
        ttl: str = "5m",
        budget: ProbeBudget | None = None,
        transport: httpx.BaseTransport | None = None,
        capability_registry: CapabilityRegistry | None = None,
        pricing_registry: PricingRegistry | None = None,
        clock: Callable[[], float] = time.time,
        run_id: str | None = None,
    ) -> None:
        pricing = pricing_registry or PricingRegistry.load_default()
        table_id = identity.pricing_table_id
        if table_id is not None and pricing.get_table(table_id) is None:
            raise ProbeConfigError(f"unknown pricing table id: {table_id}")
        self._identity = identity
        self._url = url
        self._api_key_env = api_key_env
        self._ttl = ttl
        self._budget = budget or ProbeBudget()
        self._transport = transport
        self._clock = clock
        self._run_id = run_id or new_run_id()
        self._capabilities = capability_registry or CapabilityRegistry.load_default()
        self._table = pricing.get_table(table_id) if table_id is not None else None

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def identity(self) -> EndpointIdentity:
        return self._identity

    def plan(self, scenarios: Sequence[str] | None = None) -> ProbePlan:
        """Price every requested scenario and decide which whole groups fit the budget."""
        capability = self._capabilities.lookup(
            self._identity.dialect,
            self._identity.provider,
            self._identity.model,
            self._identity.gateway_profile,
        )
        available = {
            scenario.name: scenario
            for scenario in build_scenarios(
                self._identity.dialect,
                self._identity.model,
                ttl=self._ttl,
                nonce=self._run_id,
                min_cache_tokens=capability.min_cache_tokens,
            )
        }
        names = list(scenarios) if scenarios else list(available)
        for name in names:
            if name not in available:
                raise ProbeConfigError(
                    f"scenario '{name}' does not apply to dialect '{self._identity.dialect}'"
                )

        write_tier = self._ttl if self._identity.dialect == "anthropic_messages" else None
        built: list[tuple[str, tuple[PlannedStep, ...], float | None]] = []
        for name in names:
            steps = tuple(
                self._planned_step(available[name], step, write_tier)
                for step in available[name].steps
            )
            built.append((name, steps, _sum_or_none([step.worst_case_usd for step in steps])))

        fits = select_scenarios(
            [(name, len(steps), cost) for name, steps, cost in built], self._budget
        )
        return ProbePlan(
            self._run_id,
            tuple(
                PlannedScenario(name, steps, cost, fits[name]) for name, steps, cost in built
            ),
        )

    def _planned_step(
        self, scenario: ProbeScenario, step: ProbeStep, write_tier: str | None
    ) -> PlannedStep:
        return PlannedStep(
            record_id=f"{self._run_id}:{scenario.name}:{step.label}",
            scenario=scenario.name,
            label=step.label,
            intent=step.intent,
            session_id=f"{self._run_id}:{scenario.name}",
            parent_record_id=(
                f"{self._run_id}:{scenario.name}:{step.parent}" if step.parent else None
            ),
            body=step.body,
            estimated_tokens=len(step.body) // 4,
            worst_case_usd=worst_case_cost(
                self._identity,
                step.body,
                self._table,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                write_tier=write_tier,
            ),
        )

    def preflight(self, plan: ProbePlan) -> str:
        """Refuse the whole run while refusing is still free; returns the API key to send with.

        Every configuration problem is raised here, before a single request goes out, so a run
        that cannot finish never starts spending.
        """
        if not self._budget.execute:
            raise ProbeConfigError("execute is off: pass --execute to send requests")
        if not plan.planned_steps:
            if plan.unpriced:
                raise ProbeConfigError(
                    f"no pricing rule for ({self._identity.provider}, {self._identity.model}) in "
                    f"pricing table '{self._identity.pricing_table_id}': the cost of "
                    f"{', '.join(plan.unpriced)} cannot be bounded, so it will not be sent. "
                    "Pass --pricing-table with a table that prices this model."
                )
            raise ProbeConfigError(
                "no scenario fits the budget; raise --max-cost-usd or --max-requests"
            )
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise ProbeConfigError(f"environment variable {self._api_key_env} is not set")
        return api_key

    def execute(
        self,
        plan: ProbePlan,
        *,
        on_record: Callable[[CaptureRecord], None] | None = None,
    ) -> ProbeRunResult:
        """Send the planned steps in order; stop at the first budget or upstream problem.

        ``on_record`` sees each capture the moment its response arrives, so a caller can put it
        on disk before the next request is paid for.
        """
        api_key = self.preflight(plan)

        headers = self._headers(api_key)
        secrets_to_scrub = secret_fragments(headers)
        tracker = BudgetTracker(self._budget)
        records: list[CaptureRecord] = []
        stopped: str | None = None

        def keep(record: CaptureRecord) -> None:
            records.append(record)
            if on_record is not None:
                on_record(record)

        with httpx.Client(transport=self._transport, timeout=_TIMEOUT) as client:
            for step in plan.planned_steps:
                worst_case_usd = step.worst_case_usd
                assert worst_case_usd is not None  # guaranteed by the checks above
                try:
                    tracker.reserve(worst_case_usd)
                except BudgetExceededError:
                    stopped = "budget_exceeded"
                    break

                request = client.build_request(
                    "POST", self._url, content=step.body, headers=headers
                )
                sent_at = self._clock()
                try:
                    response = client.send(request)
                except httpx.HTTPError as exc:
                    tracker.settle(None, worst_case_usd)
                    keep(self._capture(step, request, None, sent_at, secrets_to_scrub))
                    stopped = f"network_error:{type(exc).__name__}"
                    break

                record = self._capture(step, request, response, sent_at, secrets_to_scrub)
                keep(record)
                usage = get_dialect_adapter(self._identity.dialect).extract_usage(record)
                actual = estimate_cost(self._identity, usage, self._table)
                tracker.settle(
                    actual.estimated_total_cost_usd if actual is not None else None,
                    worst_case_usd,
                )
                if not response.is_success:
                    stopped = f"http_{response.status_code}"
                    break

        return ProbeRunResult(tuple(records), stopped, tracker.spent_usd)

    def _headers(self, api_key: str) -> dict[str, str]:
        if self._identity.dialect == "anthropic_messages":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            if self._ttl == "1h":
                headers["anthropic-beta"] = _ANTHROPIC_TTL_BETA
        else:
            headers = {"authorization": f"Bearer {api_key}"}
        headers["content-type"] = "application/json"
        headers["accept"] = "application/json"
        return headers

    def _capture(
        self,
        step: PlannedStep,
        request: httpx.Request,
        response: httpx.Response | None,
        sent_at: float,
        secrets_to_scrub: tuple[str, ...],
    ) -> CaptureRecord:
        body: Any = None
        if response is not None:
            try:
                body = json.loads(response.content)
            except ValueError:
                body = None
        body = scrub_secrets(body, secrets_to_scrub) if isinstance(body, Mapping) else None
        response_id = body.get("id") if isinstance(body, Mapping) else None
        return CaptureRecord(
            record_id=step.record_id,
            timestamp=sent_at,
            identity=self._identity,
            request_headers=freeze_json(redact_headers(dict(request.headers))),
            captured_body_bytes=request.content,
            parsed_body=freeze_json(json.loads(step.body)),
            response_id=response_id if isinstance(response_id, str) else None,
            response_body=freeze_json(body) if body is not None else None,
            response_complete=(
                response is not None and response.is_success and body is not None
            ),
            client_session_id=step.session_id,
            parent_record_id=step.parent_record_id,
        )
