"""v0.2c audited repair gates R1-R5 (see specs/05-v0.2c-audited-repair/spec.md)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from cacherift.check.evaluator import evaluate_diagnoses
from cacherift.check.policy import CheckPolicy
from cacherift.cli import analyze_records, main
from cacherift.core.freeze import (
    canonical_project_bytes,
    freeze_json,
    order_sensitive_equal,
    sha256_hex,
)
from cacherift.core.models import CaptureRecord, ChangeKind, EndpointIdentity
from cacherift.io.capture import dump_capture_record, load_capture_records
from cacherift.repair.engine import repair_records
from cacherift.repair.rules import (
    ParentTurn,
    RuleResult,
    canonicalize_tool_key_order,
    migrate_gpt62_top_level_instructions,
    normalize_anthropic_breakpoints,
    stabilize_tool_array_order,
)

OPENAI = EndpointIdentity("openai_responses", "openai", "gpt-6.1", "/v1/responses")
GPT62 = EndpointIdentity("openai_responses", "openai", "gpt-6.2", "/v1/responses")
ANTHROPIC = EndpointIdentity("anthropic_messages", "anthropic", "claude-sonnet-4-6", "/v1/messages")
UNREGISTERED = EndpointIdentity(
    "openai_responses", "vllm-custom", "my-private-model", "/v1/responses"
)
CHAT = EndpointIdentity("openai_chat", "openai", "gpt-4o", "/v1/chat/completions")

SYSTEM = "You are a careful assistant."
SECRET = "Deploy at 2026-11-26 10:00:00; see $.tools[0] and C:\\path\\to\\x; cost 100%"


def _tool(name: str = "search", *, jitter: bool = False, description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    if jitter:  # same schema, different key order
        schema = {
            "required": schema["required"],
            "properties": schema["properties"],
            "type": "object",
        }
    tool: dict[str, Any] = {"type": "function", "name": name, "parameters": schema}
    if description:
        tool["description"] = description
    return tool


def _history(turn: int) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for earlier in range(1, turn):
        history += [
            {"role": "user", "content": f"question {earlier}"},
            {"role": "assistant", "content": f"answer {earlier}"},
        ]
    history.append({"role": "user", "content": f"question {turn}"})
    return history


def _openai_turn(
    turn: int, *, tools: tuple[dict[str, Any], ...], system: str = SYSTEM, model: str = "gpt-6.1"
) -> dict[str, Any]:
    return {
        "model": model,
        "tools": [dict(tool) for tool in tools],
        "instructions": system,
        "input": _history(turn),
    }


def _anthropic_message(text: str, *, role: str = "user", marked: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if marked:
        block["cache_control"] = {"type": "ephemeral"}
    return {"role": role, "content": [block]}


def _anthropic_history_turn(turn: int, *, keep_first_breakpoint: bool) -> dict[str, Any]:
    """Turn ``turn`` of one conversation; the first message keeps or drops its breakpoint."""
    messages = [_anthropic_message("question 1", marked=keep_first_breakpoint)]
    for earlier in range(1, turn):
        messages.append(_anthropic_message(f"answer {earlier}", role="assistant"))
        messages.append(_anthropic_message(f"question {earlier + 1}", marked=True))
    return {
        "model": "claude-sonnet-4-6",
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "tools": [
            {
                "name": "search",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": messages,
    }


def _record(
    record_id: str,
    body: dict[str, Any],
    *,
    parent: str | None,
    identity: EndpointIdentity = OPENAI,
    raw: bytes | None = None,
    session: str | None = "s",
) -> CaptureRecord:
    return CaptureRecord(
        record_id=record_id,
        timestamp=100.0,
        identity=identity,
        request_headers=freeze_json({}),
        captured_body_bytes=raw,
        parsed_body=freeze_json(json.loads(raw) if raw is not None else body),
        client_session_id=session,
        parent_record_id=parent,
    )


def _session(
    turns: list[dict[str, Any]],
    *,
    identity: EndpointIdentity = OPENAI,
    raw_turns: tuple[int, ...] = (),
) -> list[CaptureRecord]:
    """One explicit session: every turn's parent is the turn before it."""
    return [
        _record(
            f"turn-{number}",
            body,
            parent=f"turn-{number - 1}" if number > 1 else None,
            identity=identity,
            raw=json.dumps(body, indent=2).encode() if number in raw_turns else None,
        )
        for number, body in enumerate(turns, start=1)
    ]


def _write(path: Path, records: list[CaptureRecord]) -> Path:
    path.write_text(
        "".join(dump_capture_record(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def _changed(result: RuleResult | None) -> RuleResult:
    """Unpack a rule result that the test expects to be a change."""
    assert result is not None
    return result


def _errors(records: list[CaptureRecord]) -> set[str]:
    report = evaluate_diagnoses(analyze_records(records), CheckPolicy.default())
    return {finding.rule for finding in report.errors}


def _change_kinds(records: list[CaptureRecord]) -> set[str]:
    return {
        change.change_kind
        for diagnosis in analyze_records(records)
        for change in diagnosis.observed_changes
    }


SEARCH, WRITE, READ = _tool("search"), _tool("write"), _tool("read")
# name: (turns, identity, error-level rules before, error-level rules left after the repair)
Defect = tuple[Callable[[], list[dict[str, Any]]], EndpointIdentity, set[str], set[str]]
DEFECTS: dict[str, Defect] = {
    # name: (turns, identity, error rules before, error rules after the repair)
    "key_jitter": (
        lambda: [
            _openai_turn(1, tools=(SEARCH,)),
            _openai_turn(2, tools=(SEARCH,)),
            _openai_turn(3, tools=(_tool("search", jitter=True),)),
        ],
        OPENAI,
        {"reordered_existing"},
        set(),
    ),
    "array_permutation": (
        lambda: [
            _openai_turn(1, tools=(SEARCH, WRITE)),
            _openai_turn(2, tools=(SEARCH, WRITE)),
            _openai_turn(3, tools=(WRITE, SEARCH)),
        ],
        OPENAI,
        {"reordered_existing"},
        set(),
    ),
    "tool_inserted_in_the_middle": (
        lambda: [
            _openai_turn(1, tools=(SEARCH, WRITE)),
            _openai_turn(2, tools=(SEARCH, WRITE)),
            _openai_turn(3, tools=(SEARCH, READ, WRITE)),
        ],
        OPENAI,
        {"mutated_existing", "inserted_before_tail"},
        # the new tool still precedes the history: honest, and the best any reorder can do
        {"inserted_before_tail"},
    ),
    "gpt62_top_level_instructions": (
        lambda: [
            _openai_turn(1, tools=(SEARCH,), model="gpt-6.2"),
            _openai_turn(2, tools=(SEARCH,), model="gpt-6.2"),
            _openai_turn(3, tools=(SEARCH,), model="gpt-6.2"),
        ],
        GPT62,
        {"non_cacheable_top_level_instructions"},
        set(),
    ),
}


# --------------------------------------------------------------------------------------
# T2: the four whitelisted rules
# --------------------------------------------------------------------------------------


def test_rule_key_order_is_aligned_to_the_parent_not_to_alphabetical_order() -> None:
    parent = _openai_turn(2, tools=(SEARCH,))
    current = _openai_turn(3, tools=(_tool("search", jitter=True),))

    body, edits = _changed(canonicalize_tool_key_order(current, ParentTurn(parent, None)))

    assert order_sensitive_equal(body["tools"][0], parent["tools"][0])
    assert tuple(body["tools"][0]) == ("type", "name", "parameters")  # as established, not sorted
    assert tuple(body["tools"][0]["parameters"]) == ("type", "properties", "required")
    assert [e.json_path for e in edits] == ["$.tools[0]"]
    assert current["tools"][0]["parameters"]["type"] == "object"  # input not mutated


def test_rule_key_order_leaves_a_genuinely_changed_tool_alone() -> None:
    parent = _openai_turn(2, tools=(SEARCH,))
    changed = _openai_turn(3, tools=(_tool("search", description="now with a description"),))

    assert canonicalize_tool_key_order(changed, ParentTurn(parent, None)) is None
    first_turn = ParentTurn(None, None)
    assert canonicalize_tool_key_order(_openai_turn(1, tools=(SEARCH,)), first_turn) is None


def test_rule_array_order_is_restored_and_new_tools_go_last() -> None:
    parent = _openai_turn(2, tools=(SEARCH, WRITE))
    shuffled = _openai_turn(3, tools=(WRITE, READ, SEARCH))

    body, edits = _changed(stabilize_tool_array_order(shuffled, ParentTurn(parent, None)))

    assert [tool["name"] for tool in body["tools"]] == ["search", "write", "read"]
    assert body["tools"][2] is shuffled["tools"][1]  # the new tool object itself, moved not copied
    assert [e.json_path for e in edits] == ["$.tools"]
    already_stable = _openai_turn(3, tools=(SEARCH, WRITE, READ))
    assert stabilize_tool_array_order(already_stable, ParentTurn(parent, None)) is None
    # a tool the parent had and this turn dropped stays dropped
    three = _openai_turn(2, tools=(SEARCH, WRITE, READ))
    dropped_one = _openai_turn(3, tools=(READ, SEARCH))
    body, _ = _changed(stabilize_tool_array_order(dropped_one, ParentTurn(three, None)))
    assert [tool["name"] for tool in body["tools"]] == ["search", "read"]


def test_rule_a_modified_tool_with_the_same_name_keeps_its_slot_and_its_new_content() -> None:
    parent = _openai_turn(2, tools=(SEARCH, WRITE))
    modified = _tool("search", description="changed in this turn")
    current = _openai_turn(3, tools=(WRITE, modified))

    body, _ = _changed(stabilize_tool_array_order(current, ParentTurn(parent, None)))

    assert [tool["name"] for tool in body["tools"]] == ["search", "write"]
    assert body["tools"][0] is current["tools"][1]
    assert body["tools"][0]["description"] == "changed in this turn"


def test_rule_instructions_migrate_only_along_a_consistent_lineage() -> None:
    body = {
        "model": "gpt-6.2",
        "instructions": "STATIC",
        "input": [{"role": "user", "content": "q1"}],
    }

    outcome = migrate_gpt62_top_level_instructions(body, ParentTurn(None, None))
    migrated, edits = _changed(outcome)

    assert "instructions" not in migrated
    assert migrated["input"] == [{"role": "developer", "content": "STATIC"}, body["input"][0]]
    assert {e.json_path for e in edits} == {"$.instructions", "$.input[0]"}
    assert body["instructions"] == "STATIC"  # input not mutated

    moved_parent = ParentTurn({"model": "gpt-6.2", "input": []}, "STATIC")
    assert migrate_gpt62_top_level_instructions(body, moved_parent) is not None
    # The parent kept its top-level instructions (it was refused). Moving only this turn would
    # put a new unit in front of the prefix the parent established.
    unmoved_parent = ParentTurn({"instructions": "STATIC", "input": []}, "STATIC")
    assert migrate_gpt62_top_level_instructions(body, unmoved_parent) is None
    # The text is not static across turns: moving it would hide a real prefix mutation.
    other_text = ParentTurn({"model": "gpt-6.2", "input": []}, "OTHER")
    assert migrate_gpt62_top_level_instructions(body, other_text) is None
    # Appearing for the first time in the middle of a lineage is the same thing: a new unit.
    introduced = ParentTurn({"model": "gpt-6.2", "input": []}, None)
    assert migrate_gpt62_top_level_instructions(body, introduced) is None
    # `input` as a plain string is a request shape this rule does not rewrite.
    shorthand = {"instructions": "S", "input": "hi"}
    assert migrate_gpt62_top_level_instructions(shorthand, ParentTurn(None, None)) is None
    assert migrate_gpt62_top_level_instructions({"input": []}, ParentTurn(None, None)) is None
    # With server-side chaining the context is not in this request; there is nothing to align.
    chained = {**body, "previous_response_id": "resp_1"}
    assert migrate_gpt62_top_level_instructions(chained, ParentTurn(None, None)) is None


def test_rule_anthropic_stale_breakpoints_are_removed_while_the_last_one_stays() -> None:
    body = _anthropic_history_turn(2, keep_first_breakpoint=True)

    first_turn = ParentTurn(_anthropic_history_turn(1, keep_first_breakpoint=True), None)
    repaired, edits = _changed(normalize_anthropic_breakpoints(body, first_turn))

    assert "cache_control" not in repaired["messages"][0]["content"][0]
    assert repaired["messages"][2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # the stable prefix keeps its breakpoints
    assert repaired["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert repaired["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert [e.json_path for e in edits] == ["$.messages[0]"]
    # the input is not mutated
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    sliding = _anthropic_history_turn(2, keep_first_breakpoint=False)
    assert normalize_anthropic_breakpoints(sliding, ParentTurn(sliding, None)) is None


# --------------------------------------------------------------------------------------
# T3: engine and safety valves
# --------------------------------------------------------------------------------------


def test_r2_ambiguous_lineage_is_never_repaired() -> None:
    # C4: two terminals share a prefix with no metadata,
    # so the follow-up cannot say which of them is its parent
    records = [
        _record("term-a", _openai_turn(1, tools=(SEARCH,)), parent=None, session=None),
        _record("term-b", _openai_turn(1, tools=(SEARCH,)), parent=None, session=None),
        _record(
            "follow",
            {
                "model": "gpt-6.1",
                "tools": [_tool("search", jitter=True)],
                "instructions": SYSTEM,
                "input": [
                    {"role": "user", "content": "question 1"},
                    {"role": "assistant", "content": "answer 1"},
                    {"role": "user", "content": "next"},
                ],
            },
            parent=None,
            session=None,
        ),
    ]

    result = repair_records(records, analyze_records(records))

    assert result.audit[0].record_id == "follow"
    assert result.audit[0].applied is False
    assert result.audit[0].blocked_reason == "ambiguous_lineage"
    assert result.audit[0].edits == ()
    assert result.records[2] is records[2]  # left exactly as captured


@pytest.mark.parametrize("identity", [UNREGISTERED, CHAT], ids=["unregistered", "usage_only"])
def test_r2_unregistered_and_usage_only_targets_are_refused(identity: EndpointIdentity) -> None:
    def turn(number: int, *, jitter: bool = False) -> dict[str, Any]:
        tool = {"type": "function", "function": SEARCH} if identity is CHAT else dict(SEARCH)
        if jitter:  # the same tool with its keys in another order: repairable on a supported target
            tool = dict(reversed(list(tool.items())))
        body = _openai_turn(number, tools=(tool,), model=identity.model)
        if identity is CHAT:
            body.pop("instructions")
            body["messages"] = [{"role": "system", "content": SYSTEM}, *body.pop("input")]
        return body

    original = _session([turn(1), turn(2, jitter=True)], identity=identity)

    result = repair_records(original, analyze_records(original))

    assert [(e.record_id, e.applied, e.blocked_reason) for e in result.audit] == [
        ("turn-2", False, "unsupported_capability")
    ]
    assert result.records == tuple(original)


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_r3_each_repair_clears_its_break_without_adding_mutations(
    tmp_path: Path, defect: str
) -> None:
    turns, identity, before_rules, after_rules = DEFECTS[defect]
    original = _session(turns(), identity=identity)
    capture = _write(tmp_path / "capture.jsonl", original)
    repaired_path = tmp_path / "repaired.jsonl"

    assert main(["repair", str(capture), "--apply", "--output", str(repaired_path)]) == 0

    reloaded = load_capture_records(capture)
    repaired = load_capture_records(repaired_path)
    assert _errors(reloaded) == before_rules
    assert _errors(repaired) == after_rules
    assert ChangeKind.MUTATED_EXISTING not in _change_kinds(repaired)
    # untouched turns keep their exact line; repaired ones are honest projections, not wire bytes
    out_lines = repaired_path.read_text(encoding="utf-8").splitlines()
    in_lines = capture.read_text(encoding="utf-8").splitlines()
    for number, (before, after) in enumerate(zip(reloaded, repaired, strict=True)):
        same = canonical_project_bytes(before.parsed_body) == canonical_project_bytes(
            after.parsed_body
        )
        assert same or after.captured_body_bytes is None
        assert (out_lines[number] == in_lines[number]) is same


def test_r3_anthropic_stale_breakpoints_are_normalized() -> None:
    original = _session(
        [
            _anthropic_history_turn(1, keep_first_breakpoint=True),
            _anthropic_history_turn(2, keep_first_breakpoint=True),
        ],
        identity=ANTHROPIC,
    )

    result = repair_records(original, analyze_records(original))

    second = result.records[1].parsed_body
    assert "cache_control" not in second["messages"][0]["content"][0]
    assert second["messages"][2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert _errors(list(result.records)) == set()
    assert ChangeKind.MUTATED_EXISTING not in _change_kinds(list(result.records))


def _gpt62_turn(number: int, *, top_level: bool) -> dict[str, Any]:
    body = _openai_turn(number, tools=(SEARCH,), model="gpt-6.2")
    if not top_level:
        body.pop("instructions")
    return body


def test_r3_migration_never_runs_ahead_of_a_refused_parent() -> None:
    # Two roots share a prefix; the next turn cannot say which it continues, and the turn after
    # that names it explicitly. The refused turn keeps its top-level instructions, so its child
    # must keep them too: moving it alone would break the prefix it continues.
    records = [
        _record("x1", _gpt62_turn(1, top_level=True), parent=None, session=None, identity=GPT62),
        _record("x2", _gpt62_turn(1, top_level=True), parent=None, session=None, identity=GPT62),
        _record("p", _gpt62_turn(2, top_level=True), parent=None, session=None, identity=GPT62),
        _record("c", _gpt62_turn(3, top_level=True), parent="p", session=None, identity=GPT62),
    ]

    result = repair_records(records, analyze_records(records))

    refusals = {e.record_id: e.blocked_reason for e in result.audit if not e.applied}
    assert refusals == {"p": "ambiguous_lineage", "c": "parent_not_repaired"}
    assert "instructions" not in result.records[0].parsed_body  # the roots did move
    assert "instructions" in result.records[3].parsed_body  # the child did not run ahead
    kinds = _change_kinds(list(result.records))
    assert ChangeKind.MUTATED_EXISTING not in kinds and ChangeKind.DELETED_EXISTING not in kinds
    assert _errors(list(result.records)) == {"non_cacheable_top_level_instructions"}


def test_r3_instructions_first_appearing_mid_lineage_are_left_alone() -> None:
    turns = [_gpt62_turn(1, top_level=False), _gpt62_turn(2, top_level=False)]
    original = _session([*turns, _gpt62_turn(3, top_level=True)], identity=GPT62)

    result = repair_records(original, analyze_records(original))

    assert result.audit == ()
    assert result.records == tuple(original)
    assert ChangeKind.MUTATED_EXISTING not in _change_kinds(list(result.records))
    assert _errors(list(result.records)) == {"non_cacheable_top_level_instructions"}


def test_repair_survives_a_parent_whose_body_is_not_an_object() -> None:
    jittered = _openai_turn(2, tools=(_tool(jitter=True),))
    not_an_object = cast(Any, ["the", "request", "body", "is", "an", "array"])
    original = _session([not_an_object, jittered])

    result = repair_records(original, analyze_records(original))

    assert result.records[0] is original[0] and result.audit == ()


def test_r4_free_text_is_never_rewritten(tmp_path: Path) -> None:
    original = _session(
        [
            _openai_turn(1, tools=(SEARCH,)),
            _openai_turn(2, tools=(_tool("search", jitter=True),), system=SECRET),
        ]
    )
    audit_path = tmp_path / "audit.json"

    capture = _write(tmp_path / "capture.jsonl", original)
    assert main(["repair", str(capture), "--audit-out", str(audit_path)]) == 0

    result = repair_records(original, analyze_records(original))
    (entry,) = result.audit
    assert [e.json_path for e in entry.edits] == ["$.tools[0]"]
    repaired = result.records[1].parsed_body
    assert repaired["instructions"] == SECRET  # this turn's own text, not the parent's
    assert repaired["input"] == original[1].parsed_body["input"]
    # the real mutation is still visible to the diagnoser afterwards: it was not papered over
    assert ChangeKind.MUTATED_EXISTING in _change_kinds(list(result.records))
    assert "Deploy at" not in audit_path.read_text(encoding="utf-8")


def test_r5_audit_hashes_can_be_recomputed_from_input_and_output(tmp_path: Path) -> None:
    original = _session(
        DEFECTS["array_permutation"][0](),
        raw_turns=(2, 3),  # these turns have wire bytes; turn 1 is parsed-only
    )
    capture = _write(tmp_path / "capture.jsonl", original)
    repaired_path, audit_path = tmp_path / "repaired.jsonl", tmp_path / "audit.jsonl"

    argv = [
        "repair",
        str(capture),
        "--apply",
        "--output",
        str(repaired_path),
        "--audit-out",
        str(audit_path),
    ]
    assert main(argv) == 0

    repaired = load_capture_records(repaired_path)
    entries = json.loads(audit_path.read_text(encoding="utf-8"))["audit"]
    assert [entry["record_id"] for entry in entries] == ["turn-3"]
    entry = entries[0]
    before_record, after_record = original[2], repaired[2]

    def node(body: Any, path: str) -> Any:
        match = re.fullmatch(r"\$\.([A-Za-z_]+)(?:\[(\d+)\])?", path)
        assert match is not None, path
        value = body[match.group(1)]
        return value[int(match.group(2))] if match.group(2) is not None else value

    before_bytes = before_record.captured_body_bytes
    if before_bytes is None:
        before_bytes = canonical_project_bytes(before_record.parsed_body)
    after_bytes = canonical_project_bytes(after_record.parsed_body)
    assert entry["record_sha256"] == {
        "before": sha256_hex(before_bytes),
        "before_space": "wire_raw",
        "before_len": len(before_bytes),
        "after": sha256_hex(after_bytes),
        "after_space": "projected",
        "after_len": len(after_bytes),
    }
    for edit in entry["edits"]:
        before_node = canonical_project_bytes(node(before_record.parsed_body, edit["json_path"]))
        after_node = canonical_project_bytes(node(after_record.parsed_body, edit["json_path"]))
        assert edit["sha256"] == {
            "before": sha256_hex(before_node),
            "after": sha256_hex(after_node),
        }
        assert edit["len"] == {"before": len(before_node), "after": len(after_node)}
        assert edit["space"] == "projected"
        assert edit["rule"] == "stabilize_tool_array_order"
    assert "question" not in audit_path.read_text(encoding="utf-8")


def test_repair_only_runs_the_rules_it_is_told_to_run() -> None:
    original = _session(DEFECTS["key_jitter"][0]())

    diagnoses = analyze_records(original)
    only_order = repair_records(original, diagnoses, rules=["stabilize_tool_array_order"])
    both = repair_records(original, diagnoses)

    assert only_order.audit == ()  # key jitter is not an array-order problem
    assert [e.rule for e in both.audit[0].edits] == ["canonicalize_tool_key_order"]
    with pytest.raises(ValueError, match="no_such_rule"):
        repair_records(original, analyze_records(original), rules=["no_such_rule"])


def test_repair_compares_each_turn_against_its_parents_repaired_body() -> None:
    # turn 2 permutes the tools, turn 3 keeps turn 2's (wrong) order and adds one
    original = _session(
        [
            _openai_turn(1, tools=(SEARCH, WRITE)),
            _openai_turn(2, tools=(WRITE, SEARCH)),
            _openai_turn(3, tools=(WRITE, READ, SEARCH)),
        ]
    )

    result = repair_records(original, analyze_records(original))

    names = [[tool["name"] for tool in record.parsed_body["tools"]] for record in result.records]
    assert names == [
        ["search", "write"],
        ["search", "write"],
        ["search", "write", "read"],
    ]
    assert [entry.record_id for entry in result.audit] == ["turn-2", "turn-3"]
    assert _errors(list(result.records)) == {"inserted_before_tail"}


# --------------------------------------------------------------------------------------
# T4: CLI
# --------------------------------------------------------------------------------------


def test_r1_dry_run_writes_nothing_and_apply_is_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    capture = _write(tmp_path / "capture.jsonl", _session(DEFECTS["key_jitter"][0]()))
    capsys.readouterr()

    assert main(["repair", str(capture)]) == 0

    out = capsys.readouterr().out
    assert "canonicalize_tool_key_order" in out and "$.tools[0]" in out and "turn-3" in out
    assert sorted(path.name for path in tmp_path.iterdir()) == ["capture.jsonl"]

    assert main(["repair", str(capture), "--apply"]) == 2  # nowhere to write to
    assert main(["repair", str(capture), "--output", "repaired.jsonl"]) == 2  # not asked to write
    existing = tmp_path / "repaired.jsonl"
    existing.write_text("an earlier capture", encoding="utf-8")
    assert main(["repair", str(capture), "--apply", "--output", str(existing)]) == 2
    assert existing.read_text(encoding="utf-8") == "an earlier capture"
    assert main(["repair", str(capture), "--rule", "bogus"]) == 2
    assert main(["repair", str(tmp_path / "missing.jsonl")]) == 2

    # an explicit audit path is the one thing a dry-run may write
    assert main(["repair", str(capture), "--audit-out", "audit.json"]) == 0
    # repaired.jsonl is the decoy created above; the dry-run added only audit.json
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "audit.json",
        "capture.jsonl",
        "repaired.jsonl",
    ]


def test_cli_json_summary_names_rules_blocks_and_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    turns = [
        _openai_turn(1, tools=(SEARCH,)),
        _openai_turn(2, tools=(SEARCH,)),
        _openai_turn(3, tools=(_tool("search", jitter=True),)),
    ]
    capture = _write(tmp_path / "capture.jsonl", _session(turns))

    assert main(["repair", str(capture), "--format", "json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["summary"] == {"records": 3, "repaired": 1, "blocked": 0}
    assert [(e["record_id"], e["applied"], e["blocked_reason"]) for e in report["audit"]] == [
        ("turn-3", True, None)
    ]
    assert report["audit"][0]["edits"][0]["rule"] == "canonicalize_tool_key_order"


def test_cli_apply_writes_a_repaired_capture_that_analyze_and_check_read(tmp_path: Path) -> None:
    turns = DEFECTS["gpt62_top_level_instructions"][0]()
    capture = _write(tmp_path / "capture.jsonl", _session(turns, identity=GPT62))
    repaired_path = tmp_path / "repaired.jsonl"

    assert main(["repair", str(capture), "--apply", "--output", str(repaired_path)]) == 0

    assert _errors(load_capture_records(capture)) == {"non_cacheable_top_level_instructions"}
    assert _errors(load_capture_records(repaired_path)) == set()
    assert main(["check", str(repaired_path)]) == 0
