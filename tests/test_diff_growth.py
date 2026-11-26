from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cacherift.capabilities.registry import CapabilityRegistry
from cacherift.core.diff import compare_turns
from cacherift.core.freeze import freeze_json
from cacherift.core.models import (
    CaptureRecord,
    ChangeKind,
    EndpointIdentity,
    ObservedChange,
    OffsetSpace,
)
from cacherift.dialects.base import get_dialect_adapter


def _make_raw_oai_turn(
    record_id: str,
    raw_bytes: bytes | None,
    parsed: Mapping[str, Any],
    *,
    model: str = "gpt-6.1",
    origin_map: dict[str, str] | None = None,
) -> CaptureRecord:
    return CaptureRecord(
        record_id=record_id,
        timestamp=100.0,
        identity=EndpointIdentity("openai_responses", "openai", model, "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=raw_bytes,
        parsed_body=freeze_json(parsed),
        origin_map=freeze_json(origin_map or {}),
    )


def _run_compare(
    prev: CaptureRecord,
    curr: CaptureRecord,
    *,
    include_snippets: bool = False,
) -> tuple[ObservedChange, ...]:
    reg = CapabilityRegistry.load_default()
    adapter = get_dialect_adapter(curr.identity.dialect)
    prev_cap = reg.lookup(prev.identity.dialect, prev.identity.provider, prev.identity.model)
    curr_cap = reg.lookup(curr.identity.dialect, curr.identity.provider, curr.identity.model)
    prev_units, prev_cfg = adapter.extract_ordered_units(prev, prev_cap)
    curr_units, curr_cfg = adapter.extract_ordered_units(curr, curr_cap)
    return compare_turns(
        prev,
        curr,
        prev_units,
        curr_units,
        prev_cfg,
        curr_cfg,
        include_snippets=include_snippets,
    )


def test_c1_wire_only_diff_not_reported_as_prefix_mutation() -> None:
    raw_1 = b'{"model":"gpt-6.1","instructions":"Hello \\u0041","input":"hi"}'
    raw_2 = b'{\n  "model": "gpt-6.1",\n  "instructions": "Hello A",\n  "input": "hi"\n}'
    parsed = {"model": "gpt-6.1", "instructions": "Hello A", "input": "hi"}

    r1 = _make_raw_oai_turn("r1", raw_1, parsed)
    r2 = _make_raw_oai_turn("r2", raw_2, parsed)
    changes = _run_compare(r1, r2)

    assert len(changes) == 1
    assert changes[0].change_kind is ChangeKind.WIRE_ONLY_DIFF
    assert changes[0].span is not None
    assert changes[0].span.space is OffsetSpace.WIRE_RAW
    assert changes[0].before_snippet is None
    assert changes[0].after_snippet is None


def test_c2_normal_append_including_gpt62_trailing_instructions() -> None:
    for model in ("gpt-6.1", "gpt-6.2"):
        r1 = _make_raw_oai_turn(
            f"{model}-1",
            None,
            {
                "model": model,
                "tools": [{"type": "function", "name": "read_file"}],
                "instructions": "Follow rules.",
                "input": [{"role": "user", "content": "step 1"}],
            },
            model=model,
        )
        r2 = _make_raw_oai_turn(
            f"{model}-2",
            None,
            {
                "model": model,
                "tools": [{"type": "function", "name": "read_file"}],
                "instructions": "Follow rules.",
                "input": [
                    {"role": "user", "content": "step 1"},
                    {"role": "assistant", "content": "done 1"},
                    {"role": "user", "content": "step 2"},
                ],
            },
            model=model,
        )
        changes = _run_compare(r1, r2)
        assert changes
        assert all(c.change_kind is ChangeKind.NORMAL_APPEND for c in changes)
        assert all(c.span is not None and c.span.space is OffsetSpace.PROJECTED for c in changes)


def test_c7_tool_tail_insertion_before_downstream_units_is_inserted_before_tail() -> None:
    r1 = _make_raw_oai_turn(
        "r1",
        None,
        {
            "model": "gpt-6.1",
            "tools": [{"type": "function", "name": "read_file"}],
            "instructions": "Be careful.",
            "input": [{"role": "user", "content": "check"}],
        },
    )
    r2 = _make_raw_oai_turn(
        "r2",
        None,
        {
            "model": "gpt-6.1",
            "tools": [
                {"type": "function", "name": "read_file"},
                {"type": "function", "name": "grep_search"},
            ],
            "instructions": "Be careful.",
            "input": [
                {"role": "user", "content": "check"},
                {"role": "assistant", "content": "checked"},
                {"role": "user", "content": "next"},
            ],
        },
        origin_map={"$.tools[1]": "mcp:ripgrep"},
    )
    changes = _run_compare(r1, r2)
    kinds = {c.json_path: c.change_kind for c in changes}
    assert kinds["$.tools[1]"] is ChangeKind.INSERTED_BEFORE_TAIL
    assert kinds["$.input[1]"] is ChangeKind.NORMAL_APPEND
    assert kinds["$.input[2]"] is ChangeKind.NORMAL_APPEND
    assert "$.instructions" not in kinds
    tool_change = next(c for c in changes if c.json_path == "$.tools[1]")
    assert tool_change.origin == "mcp:ripgrep"


def test_reordered_keys_reordered_tools_mutated_deleted_compacted_and_config() -> None:
    # 1. Object key reorder inside tool schema
    r_key1 = _make_raw_oai_turn(
        "rk1",
        None,
        {
            "model": "gpt-6.1",
            "tools": [{"type": "function", "name": "t", "parameters": {"a": 1, "b": 2}}],
            "input": "hi",
        },
    )
    r_key2 = _make_raw_oai_turn(
        "rk2",
        None,
        {
            "model": "gpt-6.1",
            "tools": [{"type": "function", "name": "t", "parameters": {"b": 2, "a": 1}}],
            "input": "hi",
        },
    )
    assert [c.change_kind for c in _run_compare(r_key1, r_key2)] == [ChangeKind.REORDERED_EXISTING]

    # 2. Array permutation of tools
    r_arr1 = _make_raw_oai_turn(
        "ra1",
        None,
        {
            "model": "gpt-6.1",
            "tools": [{"type": "function", "name": "a"}, {"type": "function", "name": "b"}],
            "input": "hi",
        },
    )
    r_arr2 = _make_raw_oai_turn(
        "ra2",
        None,
        {
            "model": "gpt-6.1",
            "tools": [{"type": "function", "name": "b"}, {"type": "function", "name": "a"}],
            "input": "hi",
        },
    )
    arr_changes = _run_compare(r_arr1, r_arr2)
    assert any(c.change_kind is ChangeKind.REORDERED_EXISTING for c in arr_changes)

    # 3. Mutation + deletion + config change + snippet redaction toggle
    r_mut1 = _make_raw_oai_turn(
        "rm1",
        None,
        {
            "model": "gpt-6.1",
            "tools": [{"type": "function", "name": "a"}, {"type": "function", "name": "b"}],
            "instructions": "Original secret prompt.",
            "reasoning": {"effort": "medium"},
            "input": [{"role": "user", "content": "q1"}],
        },
    )
    r_mut2 = _make_raw_oai_turn(
        "rm2",
        None,
        {
            "model": "gpt-6.1",
            "tools": [{"type": "function", "name": "a"}],
            "instructions": "Edited secret prompt.",
            "reasoning": {"effort": "high"},
            "input": [{"role": "user", "content": "q1"}],
        },
    )
    changes_redacted = _run_compare(r_mut1, r_mut2, include_snippets=False)
    kinds = {c.change_kind for c in changes_redacted}
    assert ChangeKind.DELETED_EXISTING in kinds
    assert ChangeKind.MUTATED_EXISTING in kinds
    assert ChangeKind.CONFIG_CHANGED in kinds
    assert all(c.before_snippet is None and c.after_snippet is None for c in changes_redacted)

    changes_snippets = _run_compare(r_mut1, r_mut2, include_snippets=True)
    mut = next(c for c in changes_snippets if c.change_kind is ChangeKind.MUTATED_EXISTING)
    assert mut.before_snippet is not None and "Original secret prompt." in mut.before_snippet
    assert mut.after_snippet is not None and "Edited secret prompt." in mut.after_snippet

    # 4. Compaction / truncation of conversation history
    r_long = _make_raw_oai_turn(
        "rl",
        None,
        {
            "model": "gpt-6.1",
            "instructions": "Sys",
            "input": [
                {"role": "user", "content": "1"},
                {"role": "assistant", "content": "2"},
                {"role": "user", "content": "3"},
                {"role": "assistant", "content": "4"},
            ],
        },
    )
    r_compact = _make_raw_oai_turn(
        "rc",
        None,
        {
            "model": "gpt-6.1",
            "instructions": "Sys",
            "input": [{"role": "user", "content": "Summary of 1-4"}],
        },
    )
    compact_changes = _run_compare(r_long, r_compact)
    assert any(c.change_kind is ChangeKind.COMPACTED_OR_TRUNCATED for c in compact_changes)
