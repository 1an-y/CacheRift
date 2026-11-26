"""v0.1.5 controlled probe gates P1-P5 (see specs/02-v0.1.5-controlled-probe/spec.md)."""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from cacherift.capabilities.registry import CapabilityRegistry, PricingRegistry
from cacherift.cli import DiagnosisSession, main
from cacherift.core.diagnoser import diagnose_turn
from cacherift.core.diff import compare_turns
from cacherift.core.freeze import canonical_project_bytes, freeze_json, order_sensitive_equal
from cacherift.core.lineage import LineageResolver
from cacherift.core.models import (
    CaptureRecord,
    ChangeKind,
    DialectName,
    EndpointIdentity,
    EvidenceGrade,
    LineageStatus,
    OffsetSpace,
    TurnDiagnosis,
    UsageState,
)
from cacherift.dialects.base import get_dialect_adapter
from cacherift.io.capture import dump_capture_record, load_capture_records
from cacherift.probe import cli as probe_cli
from cacherift.probe.budget import (
    BudgetExceededError,
    BudgetTracker,
    ProbeBudget,
    select_scenarios,
    worst_case_cost,
)
from cacherift.probe.runner import PlannedScenario, ProbeConfigError, ProbeRunner, ProbeRunResult
from cacherift.probe.scenarios import (
    SCENARIO_ORDER,
    ProbeScenario,
    ProbeStep,
    build_scenarios,
)

TEST_KEY = "sk-TEST-SECRET-DO-NOT-LOG-0123456789"
TEST_KEY_ENV = "CACHELIFT_TEST_KEY"
DEFAULT_BUDGET = ProbeBudget(max_requests=12, max_cost_usd=0.10, execute=True)

_SYSTEM = "S" * 6000
_ROUND1 = {
    "model": "gpt-6.1",
    "instructions": _SYSTEM,
    "input": [{"role": "user", "content": "q1"}],
}
_ROUND2_TAIL_EDIT = {
    "model": "gpt-6.1",
    "instructions": _SYSTEM,
    "input": [{"role": "user", "content": "a different final question"}],
}
_ROUND2_HEAD_EDIT = {
    "model": "gpt-6.1",
    "instructions": _SYSTEM + "!",
    "input": [{"role": "user", "content": "a different final question"}],
}


def _identity(
    model: str = "gpt-6.1-sol",
    *,
    dialect: str = "openai_responses",
    pricing_table_id: str | None = "default-2026-11",
) -> EndpointIdentity:
    return EndpointIdentity(
        dialect=cast(DialectName, dialect),
        provider="anthropic" if dialect == "anthropic_messages" else "openai",
        model=model,
        endpoint_path="/v1/responses" if dialect == "openai_responses" else "/v1/messages",
        pricing_table_id=pricing_table_id,
    )


def _turn(
    record_id: str,
    body: dict[str, Any],
    *,
    parent: str | None,
    cached: int,
    timestamp: float,
    identity: EndpointIdentity | None = None,
) -> CaptureRecord:
    return CaptureRecord(
        record_id=record_id,
        timestamp=timestamp,
        identity=identity or _identity(),
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json(body),
        response_id=f"resp_{record_id}",
        response_body=freeze_json(
            {
                "id": f"resp_{record_id}",
                "usage": {
                    "input_tokens": 2000,
                    "input_tokens_details": {"cached_tokens": cached},
                    "output_tokens": 16,
                },
            }
        ),
        client_session_id="s",
        parent_record_id=parent,
    )


def _codes(diag: TurnDiagnosis) -> set[str]:
    return {cause.code for cause in diag.candidate_causes}


# --------------------------------------------------------------------------------------
# T1: a tail-only edit is not a prefix break (v0.1 diagnoser extension, P5)
# --------------------------------------------------------------------------------------


def test_tail_only_edit_with_zero_hit_keeps_unexplained_low_hit() -> None:
    _, d2 = DiagnosisSession().diagnose_all(
        [
            _turn("r1", _ROUND1, parent=None, cached=0, timestamp=10.0),
            _turn("r2", _ROUND2_TAIL_EDIT, parent="r1", cached=0, timestamp=20.0),
        ]
    )
    assert [(c.change_kind, c.json_path) for c in d2.observed_changes] == [
        (ChangeKind.MUTATED_EXISTING, "$.input[0]")
    ]
    codes = _codes(d2)
    assert "existing_prefix_mutated" in codes  # the edit itself is still reported
    assert "unexplained_low_hit" in codes  # ...but it cannot explain a zero read
    low_hit = next(cause for cause in d2.candidate_causes if cause.code == "unexplained_low_hit")
    assert "tail_edit=$.input[0]" in low_hit.supporting_evidence
    assert d2.unresolved
    assert not any(
        cause.grade is EvidenceGrade.OBSERVED and "gateway" in cause.code
        for cause in d2.candidate_causes
    )


def test_tail_only_edit_with_positive_read_reports_partial_hit() -> None:
    _, d2 = DiagnosisSession().diagnose_all(
        [
            _turn("r1", _ROUND1, parent=None, cached=0, timestamp=10.0),
            _turn("r2", _ROUND2_TAIL_EDIT, parent="r1", cached=1500, timestamp=20.0),
        ]
    )
    assert "partial_prefix_cache_hit" in _codes(d2)
    assert "unexplained_low_hit" not in _codes(d2)


def test_a_turn_without_breaking_changes_is_not_described_as_a_tail_edit() -> None:
    grown = {
        "model": "gpt-6.1",
        "instructions": _SYSTEM,
        "input": [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
    }
    _, d2 = DiagnosisSession().diagnose_all(
        [
            _turn("r1", _ROUND1, parent=None, cached=0, timestamp=10.0),
            _turn("r2", grown, parent="r1", cached=0, timestamp=20.0),
        ]
    )

    assert all(c.change_kind is ChangeKind.NORMAL_APPEND for c in d2.observed_changes)
    low_hit = next(cause for cause in d2.candidate_causes if cause.code == "unexplained_low_hit")
    assert not any(evidence.startswith("tail_edit") for evidence in low_hit.supporting_evidence)
    assert low_hit.summary.startswith("Shared prefix remained structurally unchanged")


def test_mutation_before_the_tail_still_blocks_low_hit_inference() -> None:
    _, d2 = DiagnosisSession().diagnose_all(
        [
            _turn("r1", _ROUND1, parent=None, cached=0, timestamp=10.0),
            _turn("r2", _ROUND2_HEAD_EDIT, parent="r1", cached=0, timestamp=20.0),
        ]
    )
    assert "$.instructions" in {c.json_path for c in d2.observed_changes}
    assert "existing_prefix_mutated" in _codes(d2)
    assert "unexplained_low_hit" not in _codes(d2)


def test_tail_only_edit_applies_to_the_anthropic_messages_array() -> None:
    identity = _identity("claude-sonnet-4-6", dialect="anthropic_messages")
    _, d2 = DiagnosisSession().diagnose_all(
        [
            _turn(
                "r1",
                {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "q1"}]},
                parent=None,
                cached=0,
                timestamp=10.0,
                identity=identity,
            ),
            _turn(
                "r2",
                {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "q2"}]},
                parent="r1",
                cached=0,
                timestamp=20.0,
                identity=identity,
            ),
        ]
    )
    assert [c.json_path for c in d2.observed_changes] == ["$.messages[0]"]
    assert "unexplained_low_hit" in _codes(d2)


def test_diagnose_turn_without_units_keeps_v01_behavior() -> None:
    cap = CapabilityRegistry.load_default().lookup("openai_responses", "openai", "gpt-6.1")
    adapter = get_dialect_adapter("openai_responses")
    r1 = _turn("r1", _ROUND1, parent=None, cached=0, timestamp=10.0)
    r2 = _turn("r2", _ROUND2_TAIL_EDIT, parent="r1", cached=0, timestamp=20.0)
    units1, cfg1 = adapter.extract_ordered_units(r1, cap)
    units2, cfg2 = adapter.extract_ordered_units(r2, cap)
    resolver = LineageResolver()
    resolver.resolve_and_register(r1, units1, None)
    lineage, parent = resolver.resolve_and_register(r2, units2, None)
    assert parent is not None
    diag = diagnose_turn(
        r2,
        lineage,
        parent,
        compare_turns(parent, r2, units1, units2, cfg1, cfg2),
        adapter.extract_usage(r2),
        None,
        cap,
        None,
        parent_usage=adapter.extract_usage(r1),
    )
    assert "unexplained_low_hit" not in _codes(diag)


# --------------------------------------------------------------------------------------
# T2: budget fuse (P2)
# --------------------------------------------------------------------------------------


def test_budget_tracker_trips_before_an_over_budget_request() -> None:
    tracker = BudgetTracker(ProbeBudget(max_requests=12, max_cost_usd=0.01))
    tracker.reserve(0.006)
    tracker.settle(actual_usd=0.004, worst_case_usd=0.006)
    assert tracker.requests_sent == 1
    assert tracker.spent_usd == pytest.approx(0.004)
    with pytest.raises(BudgetExceededError):
        tracker.reserve(0.0061)  # 0.004 spent + 0.0061 would exceed 0.01
    assert tracker.requests_sent == 1  # the over-budget request was never counted as sent
    tracker.reserve(0.006)
    tracker.settle(actual_usd=None, worst_case_usd=0.006)  # unknown actual keeps the envelope
    assert tracker.spent_usd == pytest.approx(0.010)


def test_budget_tracker_enforces_the_request_cap() -> None:
    tracker = BudgetTracker(ProbeBudget(max_requests=2, max_cost_usd=1.0))
    tracker.reserve(0.1)
    tracker.settle(0.05, 0.1)
    tracker.reserve(0.1)
    tracker.settle(0.05, 0.1)
    with pytest.raises(BudgetExceededError):
        tracker.reserve(0.1)
    assert tracker.requests_sent == 2


def test_worst_case_cost_uses_the_pricing_engine_and_write_tier() -> None:
    table = PricingRegistry.load_default().get_table("default-2026-11")
    assert table is not None
    body = b"x" * 4000  # 1000 estimated tokens at heuristic_bytes_div_4
    sol_cost = worst_case_cost(_identity("gpt-6.1-sol"), body, table, max_output_tokens=64)
    astra_cost = worst_case_cost(_identity("gpt-6.2-astra"), body, table, max_output_tokens=64)
    assert sol_cost == pytest.approx((1000 * 2.00 + 64 * 10.00) / 1e6)
    assert astra_cost == pytest.approx((1000 * 8.00 + 64 * 40.00) / 1e6)
    sonnet = _identity("claude-sonnet-4-6", dialect="anthropic_messages")
    five_minutes = worst_case_cost(sonnet, body, table, max_output_tokens=64, write_tier="5m")
    one_hour = worst_case_cost(sonnet, body, table, max_output_tokens=64, write_tier="1h")
    assert five_minutes == pytest.approx((1000 * 3.75 + 64 * 15.00) / 1e6)
    assert one_hour == pytest.approx((1000 * 6.00 + 64 * 15.00) / 1e6)
    assert worst_case_cost(_identity("unlisted-model"), body, table, max_output_tokens=64) is None


def test_scenarios_are_admitted_as_whole_groups() -> None:
    costs = [("a", 2, 0.006), ("b", 3, 0.004), ("c", 4, 0.010), ("d", 1, None)]
    by_cost = select_scenarios(costs, ProbeBudget(max_requests=12, max_cost_usd=0.01))
    assert by_cost == {"a": True, "b": True, "c": False, "d": False}
    by_requests = select_scenarios(costs, ProbeBudget(max_requests=4, max_cost_usd=1.0))
    assert by_requests == {"a": True, "b": False, "c": False, "d": False}


# --------------------------------------------------------------------------------------
# T3: deterministic scenarios built as raw bytes (P4 groundwork)
# --------------------------------------------------------------------------------------


def _scenario(scenarios: tuple[ProbeScenario, ...], name: str) -> ProbeScenario:
    return next(s for s in scenarios if s.name == name)


def _step(scenario: ProbeScenario, label: str) -> ProbeStep:
    return next(s for s in scenario.steps if s.label == label)


def _costs(scenario: PlannedScenario) -> list[float]:
    return [cast(float, step.worst_case_usd) for step in scenario.steps]


def test_wire_variant_parses_equal_but_differs_in_bytes() -> None:
    wire = _scenario(build_scenarios("openai_responses", "gpt-6.1"), "wire_vs_key_reorder")
    base = _step(wire, "base").body
    wire_only = _step(wire, "wire_only").body
    assert base != wire_only
    assert b"\n" not in base and b"\\u" not in base
    assert b"\n" in wire_only and b"\\u" in wire_only
    assert order_sensitive_equal(json.loads(base), json.loads(wire_only))


@pytest.mark.parametrize("dialect", ["openai_responses", "anthropic_messages"])
def test_reordered_variant_changes_only_tool_key_order(dialect: str) -> None:
    wire = _scenario(build_scenarios(dialect, "m"), "wire_vs_key_reorder")
    base = json.loads(_step(wire, "base").body)
    reordered = json.loads(_step(wire, "reordered_keys").body)
    assert not order_sensitive_equal(base["tools"][0], reordered["tools"][0])
    assert canonical_project_bytes(base["tools"][0], sort_keys=True) == canonical_project_bytes(
        reordered["tools"][0], sort_keys=True
    )
    def strip_tools(body: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in body.items() if k != "tools"}

    assert order_sensitive_equal(strip_tools(base), strip_tools(reordered))


def test_scenario_order_counts_and_dialect_scope() -> None:
    openai = build_scenarios("openai_responses", "gpt-6.1")
    anthropic = build_scenarios("anthropic_messages", "claude-sonnet-4-6")
    assert [s.name for s in openai] == [
        "baseline_identical_pair",
        "wire_vs_key_reorder",
        "tool_tail_insertion",
        "gpt62_instructions_placement",
    ]
    assert [s.name for s in anthropic] == [
        "baseline_identical_pair",
        "wire_vs_key_reorder",
        "tool_tail_insertion",
        "anthropic_breakpoints",
    ]
    assert [len(s.steps) for s in openai] == [2, 3, 2, 4]
    assert [len(s.steps) for s in anthropic] == [2, 3, 2, 3]
    assert SCENARIO_ORDER == (
        "baseline_identical_pair",
        "wire_vs_key_reorder",
        "tool_tail_insertion",
        "gpt62_instructions_placement",
        "anthropic_breakpoints",
    )


def test_shared_prefix_meets_token_floor_and_is_deterministic() -> None:
    first = build_scenarios("openai_responses", "gpt-6.1", nonce="run-a")
    assert [s.steps for s in first] == [
        s.steps for s in build_scenarios("openai_responses", "gpt-6.1", nonce="run-a")
    ]
    assert all(len(step.body) >= 2200 * 5 for s in first for step in s.steps)
    assert all(len(step.body) // 4 >= 2200 for s in first for step in s.steps)
    other = build_scenarios("openai_responses", "gpt-6.1", nonce="run-b")
    assert first[0].steps[0].body != other[0].steps[0].body  # a new run starts cold
    haiku = build_scenarios("anthropic_messages", "claude-haiku-4-5", min_cache_tokens=4096)
    assert all(len(step.body) >= 4096 * 5 for step in haiku[0].steps)
    assert all(len(step.body) // 4 >= 4096 for step in haiku[0].steps)

    custom_small = build_scenarios(
        "openai_responses", "gpt-6.1", min_cache_tokens=1024, prefix_tokens=500
    )
    assert all(500 * 5 <= len(step.body) < 1200 * 5 for step in custom_small[0].steps)
    custom_large = build_scenarios("openai_responses", "gpt-6.1", prefix_tokens=4500)
    assert all(len(step.body) >= 4500 * 5 for step in custom_large[0].steps)


def test_openai_baseline_carries_stable_prompt_cache_key_while_anthropic_omits_it() -> None:
    openai = build_scenarios("openai_responses", "gpt-6.1-sol", nonce="run-42")
    baseline = _scenario(openai, "baseline_identical_pair")
    for step in baseline.steps:
        assert json.loads(step.body)["prompt_cache_key"] == "run-42:baseline_identical_pair"
    for scenario in openai:
        if scenario.name != "baseline_identical_pair":
            for step in scenario.steps:
                assert "prompt_cache_key" not in json.loads(step.body)

    anthropic = build_scenarios("anthropic_messages", "claude-sonnet-4-6", nonce="run-42")
    ant_baseline = _scenario(anthropic, "baseline_identical_pair")
    for step in ant_baseline.steps:
        assert "prompt_cache_key" not in json.loads(step.body)


def test_anthropic_prefix_carries_cache_control_with_ttl() -> None:
    for ttl in ("5m", "1h"):
        for scenario in build_scenarios("anthropic_messages", "claude-sonnet-4-6", ttl=ttl):
            for step in scenario.steps:
                body = json.loads(step.body)
                assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": ttl}
    slide = json.loads(
        _step(
            _scenario(build_scenarios("anthropic_messages", "m"), "anthropic_breakpoints"), "slide"
        ).body
    )
    assert slide["messages"][-1]["content"][0]["cache_control"]["type"] == "ephemeral"
    assert "cache_control" not in slide["messages"][0]["content"][0]


def test_every_parent_link_points_at_a_declared_baseline() -> None:
    for scenario in build_scenarios("openai_responses", "gpt-6.1"):
        labels = {step.label for step in scenario.steps}
        for step in scenario.steps:
            assert step.parent is None or step.parent in labels
            assert step.parent != step.label
    wire = _scenario(build_scenarios("openai_responses", "gpt-6.1"), "wire_vs_key_reorder")
    assert [_step(wire, label).parent for label in ("base", "wire_only", "reordered_keys")] == [
        None,
        "base",
        "base",
    ]


# --------------------------------------------------------------------------------------
# T4: runner against an in-process upstream (P1-P5 end to end)
# --------------------------------------------------------------------------------------


def _usage_body(
    dialect: str,
    *,
    cached: int,
    total: int,
    writes: int,
    write_tier: str,
    echo: str | None,
    ok: bool,
    output_tokens: int = 16,
    cache_write_tokens: int | None = None,
    prompt_cache_key: str | None = None,
) -> dict[str, Any]:
    if not ok:
        return {"error": {"type": "invalid_request_error", "message": f"rejected key {echo}"}}
    if dialect == "anthropic_messages":
        return {
            "id": "msg_probe",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "alpha"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": total - cached - writes,
                "cache_creation_input_tokens": writes,
                "cache_read_input_tokens": cached,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": writes if write_tier == "5m" else 0,
                    "ephemeral_1h_input_tokens": writes if write_tier == "1h" else 0,
                },
                "output_tokens": output_tokens,
            },
        }
    details: dict[str, Any] = {"cached_tokens": cached}
    if cache_write_tokens is not None:
        details["cache_write_tokens"] = cache_write_tokens
    payload: dict[str, Any] = {
        "id": "resp_probe",
        "object": "response",
        "status": "completed",
        "output": [],
        "usage": {
            "input_tokens": total,
            "input_tokens_details": details,
            "output_tokens": output_tokens,
        },
    }
    if prompt_cache_key is not None:
        payload["prompt_cache_key"] = prompt_cache_key
    return payload


class _FakeUpstream:
    """httpx.MockTransport handler that records requests and answers with scripted usage."""

    def __init__(
        self,
        dialect: str,
        *,
        usage_for: Callable[[int], tuple[int, int, int]] | None = None,
        status: int = 200,
        echo_header: str | None = None,
        write_tier: str = "5m",
        error: Exception | None = None,
        output_tokens: int = 16,
        cache_write_tokens: int | None = None,
        cache_key_for: Callable[[int], str | None] | None = None,
    ) -> None:
        self.dialect = dialect
        self.usage_for = usage_for or (lambda _n: (0, 2200, 0))
        self.status = status
        self.echo_header = echo_header
        self.write_tier = write_tier
        self.error = error
        self.output_tokens = output_tokens
        self.cache_write_tokens = cache_write_tokens
        self.cache_key_for = cache_key_for
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        call_index = len(self.requests)
        cached, total, writes = self.usage_for(call_index)
        echo = request.headers.get(self.echo_header) if self.echo_header else None
        prompt_cache_key = self.cache_key_for(call_index) if self.cache_key_for else None
        return httpx.Response(
            self.status,
            json=_usage_body(
                self.dialect,
                cached=cached,
                total=total,
                writes=writes,
                write_tier=self.write_tier,
                echo=echo,
                ok=self.status < 400,
                output_tokens=self.output_tokens,
                cache_write_tokens=self.cache_write_tokens,
                prompt_cache_key=prompt_cache_key,
            ),
        )


def _runner(
    upstream: _FakeUpstream,
    *,
    model: str = "gpt-6.1-sol",
    dialect: str = "openai_responses",
    budget: ProbeBudget = DEFAULT_BUDGET,
    ttl: str = "5m",
) -> ProbeRunner:
    return ProbeRunner(
        _identity(model, dialect=dialect),
        url="https://upstream.invalid/v1/responses"
        if dialect == "openai_responses"
        else "https://upstream.invalid/v1/messages",
        api_key_env=TEST_KEY_ENV,
        ttl=ttl,
        budget=budget,
        transport=httpx.MockTransport(upstream),
        run_id="probe-test",
        clock=lambda: 1000.0,
    )


def test_p1_plan_makes_no_http_calls_and_lists_estimates() -> None:
    upstream = _FakeUpstream("openai_responses")
    plan = _runner(upstream).plan()
    assert upstream.requests == []
    assert len(plan.steps) == 11
    for scenario in plan.scenarios:
        assert scenario.worst_case_usd == pytest.approx(sum(_costs(scenario)))
    assert plan.worst_case_usd == pytest.approx(
        sum(cost for scenario in plan.scenarios for cost in _costs(scenario))
    )
    assert all(step.estimated_tokens == len(step.body) // 4 for step in plan.steps)


def test_p5_default_flagship_plan_fits_the_default_budget() -> None:
    plan = _runner(_FakeUpstream("openai_responses")).plan()
    assert [s.fits_budget for s in plan.scenarios] == [True, True, True, True]
    assert plan.worst_case_usd is not None and plan.worst_case_usd < DEFAULT_BUDGET.max_cost_usd


def test_sonnet_default_budget_admits_whole_scenarios_only() -> None:
    # Sonnet writes cost more per token, so the default cap cannot hold the fourth scenario.
    plan = _runner(
        _FakeUpstream("anthropic_messages"), model="claude-sonnet-4-6", dialect="anthropic_messages"
    ).plan()
    assert {s.name: s.fits_budget for s in plan.scenarios} == {
        "baseline_identical_pair": True,
        "wire_vs_key_reorder": True,
        "tool_tail_insertion": True,
        "anthropic_breakpoints": False,
    }
    assert plan.worst_case_usd is not None and plan.worst_case_usd < DEFAULT_BUDGET.max_cost_usd


def test_p2_scenarios_that_do_not_fit_are_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream("openai_responses")
    runner = _runner(upstream, budget=ProbeBudget(max_requests=3, max_cost_usd=0.05, execute=True))
    plan = runner.plan()
    assert [s.fits_budget for s in plan.scenarios] == [True, False, False, False]
    result = runner.execute(plan)
    assert len(upstream.requests) == 2 == len(result.records)
    assert result.stopped_reason is None


def test_p2_cost_drift_trips_the_fuse_before_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream("openai_responses", usage_for=lambda _n: (0, 1_000_000, 0))
    runner = _runner(upstream)
    result = runner.execute(runner.plan())
    assert len(upstream.requests) == 1  # real usage dwarfed the estimate: stop right there
    assert result.stopped_reason == "budget_exceeded"
    assert len(result.records) == 1


def test_p2_plan_with_nothing_fitting_refuses_to_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream("openai_responses")
    runner = _runner(upstream, budget=ProbeBudget(max_cost_usd=1e-9, execute=True))
    plan = runner.plan()
    assert plan.planned_steps == ()
    with pytest.raises(ProbeConfigError):
        runner.execute(plan)
    assert upstream.requests == []


def test_p3_credentials_never_reach_jsonl_or_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)

    def run(upstream: _FakeUpstream) -> ProbeRunResult:
        runner = _runner(
            upstream, budget=ProbeBudget(max_requests=2, max_cost_usd=0.05, execute=True)
        )
        return runner.execute(runner.plan())

    def dumped(name: str, records: tuple[CaptureRecord, ...]) -> str:
        path = tmp_path / name
        path.write_text(
            "".join(dump_capture_record(r) + "\n" for r in records), encoding="utf-8"
        )
        return path.read_text(encoding="utf-8")

    # 2xx: the key travels upstream intact; only its redaction is persisted
    ok_upstream = _FakeUpstream("openai_responses")
    ok = run(ok_upstream)
    assert ok_upstream.requests[0].headers["authorization"] == f"Bearer {TEST_KEY}"
    # an upstream error that echoes the key back must not persist it either
    rejected_upstream = _FakeUpstream(
        "openai_responses", status=401, echo_header="authorization"
    )
    rejected = run(rejected_upstream)
    assert rejected.records[0].response_body["error"]["message"] == "rejected key [REDACTED]"

    for index, result in enumerate((ok, rejected)):
        assert all(r.request_headers["authorization"] == "[REDACTED]" for r in result.records)
        assert TEST_KEY not in dumped(f"probe-{index}.jsonl", result.records)
    jsonl_path = tmp_path / "probe-0.jsonl"
    reloaded = load_capture_records(jsonl_path)[0].captured_body_bytes
    assert reloaded == ok.records[0].captured_body_bytes


def test_p3_execute_requires_the_api_key_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TEST_KEY_ENV, raising=False)
    runner = _runner(_FakeUpstream("openai_responses"))
    with pytest.raises(ProbeConfigError):
        runner.execute(runner.plan())


def test_p3_unregistered_model_cannot_be_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream("openai_responses")
    runner = _runner(upstream, model="unlisted-model")
    plan = runner.plan()
    assert all(s.worst_case_usd is None for s in plan.scenarios)
    assert plan.unpriced == tuple(s.name for s in plan.scenarios)
    with pytest.raises(ProbeConfigError, match="no pricing rule"):
        runner.execute(plan)
    assert upstream.requests == []


def test_p3_an_unpriced_model_is_reported_as_unpriced_not_as_over_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    monkeypatch.chdir(tmp_path)
    base = ["--dialect", "openai_responses", "--base-url", "https://upstream.invalid/v1"]
    extra = ["--model", "gpt-5-mini", "--pricing-table", "default-2026-11"]

    assert main(["probe", *base, *extra]) == 0
    out, err = capsys.readouterr()
    assert "unpriced" in out and "over budget" not in out
    assert "no pricing rule" in err and "--execute will refuse" in err

    # a bigger budget changes nothing, so the message must not send the user down that road
    upstream = _FakeUpstream("openai_responses")
    rc = probe_cli.run_command(
        _args(*base, *extra, "--execute", "--max-cost-usd", "1000", "--output", "p.jsonl"),
        transport=httpx.MockTransport(upstream),
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "no pricing rule" in err and "max-cost-usd" not in err
    assert upstream.requests == [] and list(tmp_path.iterdir()) == []


def test_p3_output_problems_are_refused_before_any_request_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    monkeypatch.chdir(tmp_path)
    upstream = _FakeUpstream("openai_responses")
    existing = tmp_path / "earlier.jsonl"
    existing.write_text("an earlier capture", encoding="utf-8")

    rc = probe_cli.run_command(
        _args(*_base_argv(), "--execute", "--output", str(existing)),
        transport=httpx.MockTransport(upstream),
    )
    assert rc == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert existing.read_text(encoding="utf-8") == "an earlier capture"

    rc = probe_cli.run_command(
        _args(*_base_argv(), "--execute", "--output", str(tmp_path / "no-such-dir" / "p.jsonl")),
        transport=httpx.MockTransport(upstream),
    )
    assert rc == 2
    assert "cannot create capture" in capsys.readouterr().err
    assert upstream.requests == []


def test_each_capture_is_on_disk_before_the_next_request_is_paid_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    capture = tmp_path / "probe.jsonl"

    class InspectingUpstream(_FakeUpstream):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            # request n may only go out once captures 1..n-1 are already on disk
            persisted = capture.read_text(encoding="utf-8").splitlines() if capture.exists() else []
            assert len(persisted) == len(self.requests)
            return super().__call__(request)

    upstream = InspectingUpstream("openai_responses")
    rc = probe_cli.run_command(
        _args(
            *_base_argv(),
            "--execute",
            "--scenario",
            "wire_vs_key_reorder",
            "--output",
            str(capture),
        ),
        transport=httpx.MockTransport(upstream),
    )

    assert rc == 0
    assert len(capture.read_text(encoding="utf-8").splitlines()) == 3 == len(upstream.requests)


def test_p4_request_content_equals_captured_body_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream("openai_responses")
    runner = _runner(upstream)
    plan = runner.plan()
    result = runner.execute(plan)
    assert len(upstream.requests) == len(plan.planned_steps) == len(result.records)
    for request, step, record in zip(
        upstream.requests, plan.planned_steps, result.records, strict=True
    ):
        assert request.content == step.body == record.captured_body_bytes
        assert request.headers["content-type"] == "application/json"
    cap = CapabilityRegistry.load_default().lookup("openai_responses", "openai", "gpt-6.1")
    units, _ = get_dialect_adapter("openai_responses").extract_ordered_units(result.records[2], cap)
    assert all(u.wire_span is not None and u.wire_span.space is OffsetSpace.WIRE_RAW for u in units)


def _diagnosed(
    upstream: _FakeUpstream, monkeypatch: pytest.MonkeyPatch, model: str = "gpt-6.2-sol"
) -> dict[tuple[str, str], TurnDiagnosis]:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    runner = _runner(upstream, model=model)
    plan = runner.plan()
    result = runner.execute(plan)
    by_step = dict(
        zip(
            [(s.scenario, s.label) for s in plan.planned_steps],
            DiagnosisSession().diagnose_all(result.records),
            strict=True,
        )
    )
    return by_step


def test_probe_records_diagnose_each_scenario_as_designed(monkeypatch: pytest.MonkeyPatch) -> None:
    by_step = _diagnosed(_FakeUpstream("openai_responses"), monkeypatch)
    baseline1 = by_step[("baseline_identical_pair", "round1")]
    assert baseline1.lineage.status is LineageStatus.NO_BASELINE
    assert baseline1.identity.model == "gpt-6.2-sol"

    baseline2 = by_step[("baseline_identical_pair", "round2")]
    assert baseline2.lineage.status is LineageStatus.MATCHED
    assert baseline2.lineage.parent_record_id == baseline1.record_id
    assert [c.json_path for c in baseline2.observed_changes] == ["$.input[0]"]

    wire_only = by_step[("wire_vs_key_reorder", "wire_only")]
    assert [c.change_kind for c in wire_only.observed_changes] == [ChangeKind.WIRE_ONLY_DIFF]
    assert wire_only.observed_changes[0].span is not None
    assert wire_only.observed_changes[0].span.space is OffsetSpace.WIRE_RAW

    reordered = by_step[("wire_vs_key_reorder", "reordered_keys")]
    assert reordered.lineage.parent_record_id == by_step[
        ("wire_vs_key_reorder", "base")
    ].record_id
    assert [(c.change_kind, c.json_path) for c in reordered.observed_changes] == [
        (ChangeKind.REORDERED_EXISTING, "$.tools[0]")
    ]

    tool_tail = by_step[("tool_tail_insertion", "round2")]
    kinds = {c.json_path: c.change_kind for c in tool_tail.observed_changes}
    assert kinds["$.tools[1]"] is ChangeKind.INSERTED_BEFORE_TAIL
    assert kinds["$.input[2]"] is ChangeKind.NORMAL_APPEND

    top = by_step[("gpt62_instructions_placement", "top_round2")]
    dev = by_step[("gpt62_instructions_placement", "dev_round2")]
    for diag in (top, dev):
        assert all(c.change_kind is ChangeKind.NORMAL_APPEND for c in diag.observed_changes)
    assert "non_cacheable_top_level_instructions" in _codes(top)
    assert "non_cacheable_top_level_instructions" not in _codes(dev)


def test_multi_root_scenarios_keep_independent_roots_without_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for model in ("gpt-6.1-sol", "gpt-6.2-sol"):
        by_step = _diagnosed(_FakeUpstream("openai_responses"), monkeypatch, model=model)
        top_r1 = by_step[("gpt62_instructions_placement", "top_round1")]
        dev_r1 = by_step[("gpt62_instructions_placement", "dev_round1")]
        for root in (top_r1, dev_r1):
            assert root.lineage.status is LineageStatus.NO_BASELINE
            assert root.lineage.parent_record_id is None
            assert root.observed_changes == ()

    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    capture_path = tmp_path / "gpt61-placement.jsonl"
    upstream = _FakeUpstream("openai_responses", usage_for=lambda n: (0, 2200, 0))
    rc = probe_cli.run_command(
        _args(
            *_base_argv("openai_responses", "gpt-6.1-sol"),
            "--execute",
            "--scenario",
            "gpt62_instructions_placement",
            "--output",
            str(capture_path),
        ),
        transport=httpx.MockTransport(upstream),
    )
    assert rc == 0
    assert main(["check", str(capture_path)]) == 0


def test_probe_settles_actual_cost_when_openai_returns_zero_cache_write_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream(
        "openai_responses",
        usage_for=lambda _n: (0, 2200, 0),
        output_tokens=5,
        cache_write_tokens=0,
    )
    runner = _runner(upstream, model="gpt-6.1-sol")
    plan = runner.plan(scenarios=("baseline_identical_pair",))
    result = runner.execute(plan)
    expected_actual = 2 * (2200 * 2.00 + 5 * 10.00) / 1_000_000
    assert result.spent_usd == pytest.approx(expected_actual)
    assert plan.worst_case_usd is not None and result.spent_usd < plan.worst_case_usd


def test_probe_warns_when_upstream_exceeds_max_output_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    upstream = _FakeUpstream(
        "openai_responses",
        usage_for=lambda _n: (0, 2200, 0),
        output_tokens=678,
        cache_write_tokens=0,
    )
    runner = _runner(upstream, model="gpt-6.1-sol")
    plan = runner.plan(scenarios=("baseline_identical_pair",))
    result = runner.execute(plan)
    assert len(result.warnings) == 2
    assert "678" in result.warnings[0]
    assert "64" in result.warnings[0]
    assert "baseline_identical_pair:round1" in result.warnings[0]

    capture_path = tmp_path / "over-output.jsonl"
    rc = probe_cli.run_command(
        _args(
            *_base_argv("openai_responses", "gpt-6.1-sol"),
            "--execute",
            "--scenario",
            "baseline_identical_pair",
            "--format",
            "json",
            "--output",
            str(capture_path),
        ),
        transport=httpx.MockTransport(upstream),
    )
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["warnings"] == list(result.warnings)
    assert "warning:" in captured.err and "678" in captured.err

    tight_upstream = _FakeUpstream(
        "openai_responses",
        usage_for=lambda _n: (0, 2200, 0),
        output_tokens=1000,
        cache_write_tokens=0,
    )
    tight_runner = _runner(
        tight_upstream,
        model="gpt-6.1-sol",
        budget=ProbeBudget(max_requests=12, max_cost_usd=0.014, execute=True),
    )
    tight_plan = tight_runner.plan(scenarios=("baseline_identical_pair",))
    tight_result = tight_runner.execute(tight_plan)
    assert tight_result.stopped_reason == "budget_exceeded"
    assert len(tight_result.records) == 1


def test_p5_zero_hit_baseline_keeps_unexplained_low_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_step = _diagnosed(_FakeUpstream("openai_responses"), monkeypatch)
    round2 = by_step[("baseline_identical_pair", "round2")]
    assert round2.usage.state is UsageState.COMPLETE
    assert round2.usage.buckets is not None and round2.usage.buckets.cache_read == 0
    assert "unexplained_low_hit" in _codes(round2)
    assert round2.unresolved
    assert not any(
        cause.grade is EvidenceGrade.OBSERVED and "gateway" in cause.code
        for cause in round2.candidate_causes
    )


def test_probe_baseline_diverged_echoed_key_reports_provider_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream(
        "openai_responses",
        usage_for=lambda _n: (0, 2300, 0),
        cache_key_for=lambda n: f"uuid-{n}",
    )
    runner = _runner(upstream, model="gpt-6.1-sol")
    plan = runner.plan(scenarios=("baseline_identical_pair",))
    result = runner.execute(plan)
    _, round2 = DiagnosisSession().diagnose_all(result.records)
    causes = {c.code: c for c in round2.candidate_causes}
    assert "response_cache_key_diverged" in causes
    assert causes["response_cache_key_diverged"].grade is EvidenceGrade.PROVIDER_REPORTED
    assert "unexplained_low_hit" not in causes


def test_upstream_error_stops_the_run_and_keeps_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream("openai_responses", status=401)
    runner = _runner(upstream)
    result = runner.execute(runner.plan())
    assert len(upstream.requests) == 1
    assert result.stopped_reason == "http_401"
    (record,) = result.records
    assert record.response_complete is False  # no successful model response, so no usage
    usage = get_dialect_adapter("openai_responses").extract_usage(record)
    assert usage.state is UsageState.MISSING and usage.buckets is None


def test_network_error_is_recorded_as_an_interrupted_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    upstream = _FakeUpstream("openai_responses", error=httpx.ConnectError("connection refused"))
    runner = _runner(upstream)
    result = runner.execute(runner.plan())
    assert result.stopped_reason == "network_error:ConnectError"
    (record,) = result.records
    assert record.response_complete is False and record.response_body is None


# --------------------------------------------------------------------------------------
# T5: `cacherift probe` CLI
# --------------------------------------------------------------------------------------


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    probe_cli.register_parser(parser.add_subparsers())
    return parser.parse_args(["probe", *argv])


def _base_argv(dialect: str = "openai_responses", model: str = "gpt-6.1-sol") -> list[str]:
    return ["--dialect", dialect, "--base-url", "https://upstream.invalid/v1", "--model", model]


def test_p1_cli_defaults_to_dry_run_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    upstream = _FakeUpstream("openai_responses")
    rc = probe_cli.run_command(
        _args(*_base_argv()), transport=httpx.MockTransport(upstream)
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert upstream.requests == []
    assert list(tmp_path.iterdir()) == []
    assert "dry-run" in out and "CacheRift probe corpus" not in out
    # the real entry point needs neither a key nor a network in dry-run mode
    assert main(["probe", *_base_argv()]) == 0


def test_p1_cli_json_dry_run_lists_every_step(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["probe", *_base_argv(), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["mode"] == "dry-run"
    steps = [step for scenario in report["scenarios"] for step in scenario["steps"]]
    assert len(steps) == 11
    assert report["totals"]["requests"] == 11
    assert report["totals"]["worst_case_usd"] == pytest.approx(
        sum(step["worst_case_usd"] for step in steps)
    )
    assert all(step["estimated_tokens"] == step["bytes"] // 4 for step in steps)
    assert report["budget"] == {"max_requests": 12, "max_cost_usd": 0.10}


def test_p1_cli_prefix_tokens_scales_dry_run_bytes(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["probe", *_base_argv(), "--prefix-tokens", "4500", "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    steps = [step for scenario in report["scenarios"] for step in scenario["steps"]]
    assert all(step["bytes"] >= 4500 * 5 for step in steps)


def test_cli_execute_writes_jsonl_and_reports_without_leaking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    monkeypatch.chdir(tmp_path)
    upstream = _FakeUpstream("openai_responses", echo_header="authorization")
    rc = probe_cli.run_command(
        _args(*_base_argv(), "--execute", "--scenario", "baseline_identical_pair"),
        transport=httpx.MockTransport(upstream),
    )
    out = capsys.readouterr().out
    assert rc == 0
    (path,) = tmp_path.iterdir()
    assert path.name.startswith("cacherift-probe-") and path.suffix == ".jsonl"
    dumped = path.read_text(encoding="utf-8")
    assert len(dumped.splitlines()) == 2 == len(upstream.requests)
    assert TEST_KEY not in dumped and TEST_KEY not in out
    assert "unexplained_low_hit" in out  # the fake upstream never hits, so P5 shows up here too

    rc = probe_cli.run_command(
        _args(
            *_base_argv(),
            "--execute",
            "--scenario",
            "baseline_identical_pair",
            "--format",
            "json",
            "--output",
            str(tmp_path / "out.jsonl"),
        ),
        transport=httpx.MockTransport(upstream),
    )
    out = capsys.readouterr().out
    report = json.loads(out)
    assert rc == 0
    assert report["mode"] == "execute"
    assert [t["lineage"]["status"] for t in report["turns"]] == ["no_baseline", "matched"]
    assert TEST_KEY not in out


def test_cli_reports_budget_skipped_scenarios_with_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    monkeypatch.chdir(tmp_path)
    upstream = _FakeUpstream("openai_responses")
    rc = probe_cli.run_command(
        _args(*_base_argv(), "--execute", "--max-requests", "3"),
        transport=httpx.MockTransport(upstream),
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert len(upstream.requests) == 2
    assert "wire_vs_key_reorder" in out and "skipped" in out

    rc = probe_cli.run_command(
        _args(*_base_argv(), "--execute", "--max-cost-usd", "0.000000001"),
        transport=httpx.MockTransport(upstream),
    )
    assert rc == 2
    assert len(upstream.requests) == 2  # nothing further was sent


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        (["--base-url", "https://user:pw@upstream.invalid/v1"], "userinfo"),
        (["--base-url", "https://upstream.invalid/v1?key=abc"], "query"),
        (["--base-url", "ftp://upstream.invalid/v1"], "http"),
        (["--scenario", "anthropic_breakpoints"], "anthropic_breakpoints"),
        (["--pricing-table", "no-such-table"], "no-such-table"),
        (["--max-requests", "0"], "--max-requests"),
        (["--max-cost-usd", "0"], "--max-cost-usd"),
        (["--prefix-tokens", "0"], "--prefix-tokens"),
        (["--prefix-tokens", "-1"], "--prefix-tokens"),
    ],
)
def test_cli_rejects_invalid_configuration(
    capsys: pytest.CaptureFixture[str], extra: list[str], fragment: str
) -> None:
    rc = main(["probe", *_base_argv(), *extra])  # argparse keeps the last value of an option
    err = capsys.readouterr().err
    assert rc == 2
    assert fragment in err


def test_cli_execute_without_api_key_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    upstream = _FakeUpstream("openai_responses")
    rc = probe_cli.run_command(
        _args(*_base_argv(), "--execute"), transport=httpx.MockTransport(upstream)
    )
    capsys.readouterr()
    assert rc == 2
    assert upstream.requests == []
    assert list(tmp_path.iterdir()) == []


def test_cli_anthropic_execute_reports_tiered_write_buckets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", TEST_KEY)
    upstream = _FakeUpstream(
        "anthropic_messages",
        usage_for=lambda n: (0, 1500, 1500) if n == 1 else (1500, 1600, 0),
        write_tier="1h",
        echo_header="x-api-key",
    )
    rc = probe_cli.run_command(
        _args(
            *_base_argv("anthropic_messages", "claude-sonnet-4-6"),
            "--execute",
            "--scenario",
            "anthropic_breakpoints",
            "--ttl",
            "1h",
            "--output",
            str(tmp_path / "anthropic.jsonl"),
            "--format",
            "json",
        ),
        transport=httpx.MockTransport(upstream),
    )
    out = capsys.readouterr().out
    assert rc == 0
    first = upstream.requests[0].headers
    assert first["x-api-key"] == TEST_KEY
    assert first["anthropic-version"] == "2023-06-01"
    assert first["anthropic-beta"] == "extended-cache-ttl-2025-04-11"
    assert "authorization" not in first
    report = json.loads(out)
    writes = report["turns"][0]["usage"]["buckets"]["cache_writes"]
    assert writes == [{"tier": "1h", "tokens": 1500}]
    assert TEST_KEY not in out


def test_probe_without_httpx_prints_one_line_hint_and_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, package: str | None = None) -> Any:
        return None if name == "httpx" else real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    rc = main(["probe", *_base_argv()])
    err = capsys.readouterr().err
    assert rc == 2
    assert err.count("\n") == 1
    assert "httpx" in err and "cacherift[probe]" in err
