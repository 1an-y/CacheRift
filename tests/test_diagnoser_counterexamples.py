from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cacherift.capabilities.registry import CapabilityRegistry
from cacherift.core.diagnoser import diagnose_turn
from cacherift.core.diff import compare_turns
from cacherift.core.freeze import freeze_json
from cacherift.core.lineage import LineageResolver
from cacherift.core.models import (
    CacheConfigFingerprint,
    CaptureRecord,
    ChangeKind,
    EndpointIdentity,
    EvidenceGrade,
    LineageStatus,
    ObservedChange,
    OffsetSpace,
    OrderedUnit,
    TurnDiagnosis,
    UsageObservation,
    UsageState,
)
from cacherift.dialects.base import get_dialect_adapter


def _diagnose_sequence(
    records: list[CaptureRecord],
    *,
    include_snippets: bool = False,
) -> list[TurnDiagnosis]:
    reg = CapabilityRegistry.load_default()
    resolver = LineageResolver()
    units_by_id: dict[str, tuple[OrderedUnit, ...]] = {}
    cfg_by_id: dict[str, CacheConfigFingerprint] = {}
    usage_by_id: dict[str, UsageObservation] = {}
    results: list[TurnDiagnosis] = []

    for rec in records:
        cap = reg.lookup(
            rec.identity.dialect,
            rec.identity.provider,
            rec.identity.model,
            rec.identity.gateway_profile,
        )
        adapter = get_dialect_adapter(rec.identity.dialect)
        units, cfg = adapter.extract_ordered_units(rec, cap)
        prev_ref = adapter.extract_previous_response_ref(rec)
        lineage, parent = resolver.resolve_and_register(rec, units, prev_ref)
        usage = adapter.extract_usage(rec)
        p_diag = adapter.extract_provider_diagnostics(rec)

        changes: tuple[ObservedChange, ...] = ()
        parent_usage: UsageObservation | None = None
        if parent is not None:
            changes = compare_turns(
                parent,
                rec,
                units_by_id[parent.record_id],
                units,
                cfg_by_id[parent.record_id],
                cfg,
                include_snippets=include_snippets,
            )
            parent_usage = usage_by_id[parent.record_id]

        diag = diagnose_turn(
            rec,
            lineage,
            parent,
            changes,
            usage,
            p_diag,
            cap,
            None,
            parent_usage=parent_usage,
        )
        units_by_id[rec.record_id] = units
        cfg_by_id[rec.record_id] = cfg
        usage_by_id[rec.record_id] = usage
        results.append(diag)
    return results


def _oai_rec(
    record_id: str,
    ts: float,
    body: Mapping[str, Any],
    *,
    raw_bytes: bytes | None = None,
    cached_tokens: int | None = 0,
    input_tokens: int | None = 1500,
    output_tokens: int | None = 50,
    response_complete: bool = True,
    client_session_id: str | None = None,
    response_id: str | None = None,
    provider_diag: Mapping[str, Any] | None = None,
    model: str = "gpt-6.1",
) -> CaptureRecord:
    resp_body: dict[str, object] | None = None
    if response_complete and input_tokens is not None and output_tokens is not None:
        usage_obj: dict[str, object] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if cached_tokens is not None:
            usage_obj["input_tokens_details"] = {"cached_tokens": cached_tokens}
        resp_body = {"id": response_id or f"resp_{record_id}", "usage": usage_obj}
        if provider_diag is not None:
            resp_body["prompt_cache_diagnostics"] = provider_diag
    return CaptureRecord(
        record_id=record_id,
        timestamp=ts,
        identity=EndpointIdentity("openai_responses", "openai", model, "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=raw_bytes,
        parsed_body=freeze_json(body),
        response_id=response_id,
        response_body=freeze_json(resp_body) if resp_body is not None else None,
        response_complete=response_complete,
        client_session_id=client_session_id,
    )


def test_c1_wire_only_diff() -> None:
    raw1 = (
        b'{"model":"gpt-6.1","instructions":"Sys \\u0041","input":[{"role":"user","content":"hi"}]}'
    )
    raw2 = (
        b'{\n  "model": "gpt-6.1",\n  "instructions": "Sys A",\n'
        b'  "input": [{"role": "user", "content": "hi"}]\n}'
    )
    parsed = {
        "model": "gpt-6.1",
        "instructions": "Sys A",
        "input": [{"role": "user", "content": "hi"}],
    }
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c1-1", 10.0, parsed, raw_bytes=raw1, cached_tokens=0, client_session_id="s-c1"
            ),
            _oai_rec(
                "c1-2", 20.0, parsed, raw_bytes=raw2, cached_tokens=1400, client_session_id="s-c1"
            ),
        ]
    )
    d2 = diags[1]
    assert [c.change_kind for c in d2.observed_changes] == [ChangeKind.WIRE_ONLY_DIFF]
    assert d2.observed_changes[0].span is not None
    assert d2.observed_changes[0].span.space is OffsetSpace.WIRE_RAW
    assert not any(cause.code == "existing_prefix_mutated" for cause in d2.candidate_causes)


def test_c2_normal_multi_turn_append() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c2-1",
                10.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [{"role": "user", "content": "u1"}],
                },
                cached_tokens=0,
            ),
            _oai_rec(
                "c2-2",
                20.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [
                        {"role": "user", "content": "u1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "user", "content": "u2"},
                    ],
                },
                cached_tokens=1400,
                input_tokens=1600,
            ),
        ]
    )
    d2 = diags[1]
    assert d2.observed_changes
    assert all(c.change_kind is ChangeKind.NORMAL_APPEND for c in d2.observed_changes)
    assert not any(cause.code == "existing_prefix_mutated" for cause in d2.candidate_causes)


def test_c3_first_captured_request_with_positive_cache_read() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c3-1",
                10.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [{"role": "user", "content": "hello"}],
                },
                cached_tokens=1200,
                input_tokens=1500,
            )
        ]
    )
    d1 = diags[0]
    assert d1.lineage.status is LineageStatus.NO_BASELINE
    assert d1.usage.buckets is not None and d1.usage.buckets.cache_read == 1200
    assert not any("cold_start" in cause.code for cause in d1.candidate_causes)
    assert any(cause.code == "warm_cache_without_local_baseline" for cause in d1.candidate_causes)


def test_c4_ambiguous_dual_terminals() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "term-a",
                10.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [{"role": "user", "content": "go"}],
                },
            ),
            _oai_rec(
                "term-b",
                11.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [{"role": "user", "content": "go"}],
                },
            ),
            _oai_rec(
                "follow",
                15.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [
                        {"role": "user", "content": "go"},
                        {"role": "assistant", "content": "ok"},
                        {"role": "user", "content": "next"},
                    ],
                },
            ),
        ]
    )
    df = diags[2]
    assert df.lineage.status is LineageStatus.AMBIGUOUS
    assert df.lineage.parent_record_id is None
    assert set(df.lineage.candidate_parent_ids) == {"term-a", "term-b"}
    assert df.unresolved


def test_c5_tail_change_coexists_with_partial_cache_hit() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c5-1",
                10.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [
                        {"role": "user", "content": "u1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "user", "content": "old tail"},
                    ],
                },
                cached_tokens=0,
                client_session_id="s-c5",
            ),
            _oai_rec(
                "c5-2",
                20.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [
                        {"role": "user", "content": "u1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "user", "content": "revised tail"},
                    ],
                },
                cached_tokens=1100,
                input_tokens=1500,
                client_session_id="s-c5",
            ),
        ]
    )
    d2 = diags[1]
    assert any(c.change_kind is ChangeKind.MUTATED_EXISTING for c in d2.observed_changes)
    assert d2.usage.buckets is not None and d2.usage.buckets.cache_read == 1100
    assert any(cause.code == "partial_prefix_cache_hit" for cause in d2.candidate_causes)
    assert any(cause.code == "existing_prefix_mutated" for cause in d2.candidate_causes)


def test_c6_interrupted_and_missing_usage() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c6-interrupted",
                10.0,
                {"model": "gpt-6.1", "input": "hi"},
                response_complete=False,
            ),
            _oai_rec(
                "c6-missing",
                20.0,
                {"model": "gpt-6.1", "input": "hi"},
                input_tokens=None,
                output_tokens=None,
                response_complete=True,
            ),
        ]
    )
    assert diags[0].usage.state is UsageState.MISSING
    assert diags[0].usage.buckets is None
    assert diags[1].usage.state is UsageState.MISSING
    assert diags[1].usage.buckets is None


def test_c7_tool_tail_insertion_before_downstream() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c7-1",
                10.0,
                {
                    "model": "gpt-6.1",
                    "tools": [{"type": "function", "name": "t1"}],
                    "instructions": "Sys",
                    "input": [{"role": "user", "content": "hi"}],
                },
                cached_tokens=0,
            ),
            _oai_rec(
                "c7-2",
                20.0,
                {
                    "model": "gpt-6.1",
                    "tools": [
                        {"type": "function", "name": "t1"},
                        {"type": "function", "name": "t2"},
                    ],
                    "instructions": "Sys",
                    "input": [
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "ok"},
                        {"role": "user", "content": "next"},
                    ],
                },
                cached_tokens=400,
            ),
        ]
    )
    d2 = diags[1]
    assert any(c.change_kind is ChangeKind.INSERTED_BEFORE_TAIL for c in d2.observed_changes)
    assert any(
        cause.code == "upstream_layer_inserted" and cause.grade is EvidenceGrade.OBSERVED
        for cause in d2.candidate_causes
    )


def test_c8_consecutive_zero_cache_read_unexplained_low_hit() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c8-1",
                10.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [{"role": "user", "content": "u1"}],
                },
                cached_tokens=0,
                input_tokens=2000,
                client_session_id="s-c8",
            ),
            _oai_rec(
                "c8-2",
                25.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Sys",
                    "input": [
                        {"role": "user", "content": "u1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "user", "content": "u2"},
                    ],
                },
                cached_tokens=0,
                input_tokens=2200,
                client_session_id="s-c8",
            ),
        ]
    )
    d2 = diags[1]
    assert any(cause.code == "unexplained_low_hit" for cause in d2.candidate_causes)
    assert len(d2.candidate_causes) >= 2
    assert all(
        cause.grade is not EvidenceGrade.OBSERVED or cause.code != "gateway_stripped_params"
        for cause in d2.candidate_causes
    )
    assert d2.unresolved


def test_c9_simultaneous_prefix_mutation_and_ttl_expiry() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c9-1",
                1000.0,
                {
                    "model": "gpt-6.1",
                    "instructions": "Old Sys",
                    "input": [{"role": "user", "content": "u1"}],
                },
                cached_tokens=0,
                client_session_id="s-c9",
            ),
            _oai_rec(
                "c9-2",
                2000.0,  # 1000s > 300s default hint
                {
                    "model": "gpt-6.1",
                    "instructions": "New Sys",
                    "input": [{"role": "user", "content": "u1"}],
                },
                cached_tokens=0,
                client_session_id="s-c9",
            ),
        ]
    )
    d2 = diags[1]
    codes = {cause.code for cause in d2.candidate_causes}
    assert "existing_prefix_mutated" in codes
    assert "possible_ttl_expiry" in codes
    assert d2.elapsed_since_parent_seconds == 1000.0


def test_c10_parsed_only_projected_space_zero_snippets_and_provider_diag() -> None:
    diags = _diagnose_sequence(
        [
            _oai_rec(
                "c10-1",
                10.0,
                {
                    "model": "gpt-6.2",
                    "instructions": "Secret instructions",
                    "input": [{"role": "user", "content": "u1"}],
                },
                raw_bytes=None,
                cached_tokens=0,
                model="gpt-6.2",
                client_session_id="s-c10",
            ),
            _oai_rec(
                "c10-2",
                20.0,
                {
                    "model": "gpt-6.2",
                    "instructions": "Updated secret",
                    "input": [{"role": "user", "content": "u1"}],
                },
                raw_bytes=None,
                cached_tokens=0,
                model="gpt-6.2",
                client_session_id="s-c10",
                provider_diag={
                    "type": "cache_miss",
                    "reason": "instructions_changed",
                    "divergences": ["instructions"],
                },
            ),
        ],
        include_snippets=False,
    )
    d2 = diags[1]
    assert d2.observed_changes
    assert all(
        c.span is not None and c.span.space is OffsetSpace.PROJECTED for c in d2.observed_changes
    )
    assert all(c.before_snippet is None and c.after_snippet is None for c in d2.observed_changes)
    assert any(cause.grade is EvidenceGrade.PROVIDER_REPORTED for cause in d2.candidate_causes)
    assert any(
        cause.code == "non_cacheable_top_level_instructions" for cause in d2.candidate_causes
    )


def test_cli_analyze_default_redaction_json_and_pricing(tmp_path: object, capsys: object) -> None:
    import json
    from pathlib import Path

    from cacherift import __version__
    from cacherift.cli import main
    from cacherift.io.capture import dump_capture_record

    assert isinstance(tmp_path, Path)

    secret_old = "TOP_SECRET_SYSTEM_PROMPT_ALPHA_98765"
    secret_new = "TOP_SECRET_SYSTEM_PROMPT_BETA_43210"
    r1 = _oai_rec(
        "cli-1",
        10.0,
        {
            "model": "gpt-6.1",
            "instructions": secret_old,
            "input": [{"role": "user", "content": "hi"}],
        },
        raw_bytes=None,
        cached_tokens=0,
        input_tokens=1500,
        output_tokens=50,
        client_session_id="s-cli",
    )
    r2 = _oai_rec(
        "cli-2",
        20.0,
        {
            "model": "gpt-6.1",
            "instructions": secret_new,
            "input": [{"role": "user", "content": "hi"}],
        },
        raw_bytes=None,
        cached_tokens=200,
        input_tokens=1500,
        output_tokens=60,
        client_session_id="s-cli",
    )
    jsonl_path = tmp_path / "cli_test.jsonl"
    jsonl_path.write_text(
        dump_capture_record(r1) + "\n" + dump_capture_record(r2) + "\n", encoding="utf-8"
    )

    # --version
    rc = main(["--version"])
    assert rc == 0
    out_ver = capsys.readouterr().out  # type: ignore[attr-defined]
    assert __version__ in out_ver

    # Default JSON report: zero snippets leaked, PROJECTED coordinate space, pricing estimate
    rc = main(
        ["analyze", str(jsonl_path), "--format", "json", "--pricing-table", "default-2026-11"]
    )
    assert rc == 0
    raw_json = capsys.readouterr().out  # type: ignore[attr-defined]
    assert secret_old not in raw_json
    assert secret_new not in raw_json

    report = json.loads(raw_json)
    assert len(report["turns"]) == 2
    t2 = report["turns"][1]
    assert t2["observed_changes"][0]["span"]["space"] == "projected"
    assert t2["observed_changes"][0]["before_snippet"] is None
    assert t2["observed_changes"][0]["after_snippet"] is None
    assert t2["cost_estimate"]["pricing_table_id"] == "default-2026-11"

    # Default table mode also must not leak snippets
    rc = main(["analyze", str(jsonl_path)])
    assert rc == 0
    table_out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert secret_old not in table_out
    assert secret_new not in table_out

    # --include-snippets explicitly opts in to snippets
    rc = main(["analyze", str(jsonl_path), "--format", "json", "--include-snippets"])
    assert rc == 0
    snippet_json = capsys.readouterr().out  # type: ignore[attr-defined]
    assert secret_old in snippet_json
    assert secret_new in snippet_json
