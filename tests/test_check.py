"""v0.2b CI check gates K1-K5 (see specs/04-v0.2b-ci-check/spec.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cacherift.check.evaluator import CheckReport, evaluate_diagnoses, path_matches
from cacherift.check.policy import ALL_RULES, CheckPolicy, PolicyError, load_policy
from cacherift.check.pytest_plugin import PrefixSession, assert_prefix_stable
from cacherift.cli import analyze_records, main
from cacherift.core.freeze import freeze_json
from cacherift.core.models import CaptureRecord, EndpointIdentity
from cacherift.io.capture import dump_capture_record, load_capture_records

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLIDING_BREAKPOINTS = "$.messages[*].content[*].cache_control"
STRICT = CheckPolicy(error=frozenset({"config_changed"}), warning=frozenset(), ignore=frozenset())
ALLOW_EVERYTHING = CheckPolicy(error=frozenset(), warning=frozenset(), ignore=frozenset())


def _record(
    record_id: str,
    body: dict[str, Any],
    *,
    parent: str | None,
    raw: bytes | None = None,
    session: str = "s",
    usage: dict[str, Any] | None = None,
    identity: EndpointIdentity | None = None,
) -> CaptureRecord:
    response = {"id": f"resp_{record_id}", "usage": usage} if usage is not None else None
    return CaptureRecord(
        record_id=record_id,
        timestamp=100.0,
        identity=identity
        or EndpointIdentity("openai_responses", "openai", "gpt-6.1", "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=raw,
        parsed_body=freeze_json(json.loads(raw) if raw is not None else body),
        response_id=f"resp_{record_id}" if response is not None else None,
        response_body=freeze_json(response) if response is not None else None,
        client_session_id=session,
        parent_record_id=parent,
    )


def _dump(tmp_path: Path, records: list[CaptureRecord], name: str = "capture.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        "".join(dump_capture_record(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def _usage(cached: int = 0, total: int = 1500) -> dict[str, Any]:
    return {
        "input_tokens": total,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": 10,
    }


def _tool(jitter: bool = False) -> dict[str, Any]:
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
    return {"type": "function", "name": "search", "parameters": schema}


def _body(
    turn: int, *, tools: tuple[dict[str, Any], ...] | None = None, system: str = "Be careful."
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    for earlier in range(1, turn):
        history += [
            {"role": "user", "content": f"question {earlier}"},
            {"role": "assistant", "content": f"answer {earlier}"},
        ]
    history.append({"role": "user", "content": f"question {turn}"})
    body: dict[str, Any] = {"model": "gpt-6.1", "instructions": system}
    if tools is not None:
        body["tools"] = [dict(tool) for tool in tools]
    body["input"] = history
    return body


def _session(turns: list[dict[str, Any]]) -> list[CaptureRecord]:
    return [
        _record(f"turn-{number:04d}", body, parent=f"turn-{number - 1:04d}" if number > 1 else None)
        for number, body in enumerate(turns, start=1)
    ]


def _broken(kind: str, secret: str = "") -> list[CaptureRecord]:
    """Three turns of one session whose third turn breaks the prefix in the named way."""
    search = _tool()
    first_two = [_body(1, tools=(search,)), _body(2, tools=(search,))]
    if kind == "tool_key_jitter":
        return _session([*first_two, _body(3, tools=(_tool(jitter=True),))])
    if kind == "tool_inserted":
        write = {"type": "function", "name": "write", "parameters": {"type": "object"}}
        return _session([*first_two, _body(3, tools=(search, write))])
    return _session([*first_two, _body(3, tools=(search,), system=secret)])


EXPECTED = {
    "tool_key_jitter": ("reordered_existing", "$.tools[0]"),
    "tool_inserted": ("inserted_before_tail", "$.tools[1]"),
    "system_prompt_edited": ("mutated_existing", "$.instructions"),
}


# --------------------------------------------------------------------------------------
# T1: policy
# --------------------------------------------------------------------------------------


def test_policy_default_severities() -> None:
    policy = CheckPolicy.default()

    def at(level: str) -> list[str]:
        return sorted(rule for rule in ALL_RULES if policy.severity(rule) == level)

    assert at("error") == sorted(
        [
            "mutated_existing",
            "reordered_existing",
            "deleted_existing",
            "inserted_before_tail",
            "non_cacheable_top_level_instructions",
            "usage_invalid",
        ]
    )
    assert at("warning") == sorted(
        [
            "compacted_or_truncated",
            "config_changed",
            "wire_only_diff",
            "ambiguous_lineage",
            "unexplained_low_hit",
        ]
    )
    assert at("ignore") == []
    # normal growth, a warm first turn, and the first turn itself never block
    for allowed in ("normal_append", "warm_cache_without_local_baseline", "no_baseline"):
        assert policy.severity(allowed) is None


def test_policy_config_moves_only_the_rules_it_names() -> None:
    policy = CheckPolicy.from_mapping(
        {
            "error": ["wire_only_diff", "usage_missing"],
            "ignore": ["wire_only_diff", "mutated_existing"],
            "ignore_paths": ["$.tools[*]"],
        }
    )

    assert policy.severity("wire_only_diff") == "ignore"  # named twice: ignore outranks error
    assert policy.severity("mutated_existing") == "ignore"
    assert policy.severity("usage_missing") == "error"
    assert policy.severity("reordered_existing") == "error"  # not named: keeps its default
    assert policy.severity("compacted_or_truncated") == "warning"
    assert policy.ignore_paths == ("$.tools[*]",)
    demoted = CheckPolicy.from_mapping({"warning": ["mutated_existing"]})
    assert demoted.severity("mutated_existing") == "warning"


def test_policy_echo_cause_codes_are_aliases_of_their_change_kind() -> None:
    policy = CheckPolicy.from_mapping({"error": ["existing_prefix_mutated"]})

    assert policy.severity("existing_prefix_mutated") == "error"
    assert policy.severity("mutated_existing") == "error"


def test_policy_rejects_unknown_rule_ids() -> None:
    with pytest.raises(PolicyError) as excinfo:
        CheckPolicy.from_mapping({"error": ["mutated_existing", "mutated_existent"]})

    message = str(excinfo.value)
    assert "mutated_existent" in message and "inserted_before_tail" in message
    with pytest.raises(PolicyError):
        CheckPolicy.from_mapping({"surprise": []})


@pytest.mark.parametrize(
    ("filename", "table"),
    [("cacherift.toml", "[check]"), ("pyproject.toml", "[tool.cacherift.check]")],
)
def test_policy_is_loaded_from_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, table: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / filename).write_text(
        f'{table}\nwarning = ["mutated_existing"]\nignore_paths = [\'$.tools[*]\']\n',
        encoding="utf-8",
    )

    policy = load_policy(None, start=tmp_path)

    assert policy.severity("mutated_existing") == "warning"  # demoted from its default error
    assert policy.severity("reordered_existing") == "error"  # not named: keeps its default
    assert policy.ignore_paths == ("$.tools[*]",)


def test_policy_without_any_config_is_the_default(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    assert load_policy(None, start=tmp_path) == CheckPolicy.default()


def test_policy_explicit_config_must_exist_and_hold_a_table(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "missing.toml", start=tmp_path)
    (tmp_path / "cacherift.toml").write_text("[other]\nx = 1\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "cacherift.toml", start=tmp_path)


# --------------------------------------------------------------------------------------
# T2: evaluator
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        (SLIDING_BREAKPOINTS, "$.messages[2].content[0].cache_control", True),
        (SLIDING_BREAKPOINTS, "$.messages[2].content[0]", False),
        (SLIDING_BREAKPOINTS, "$.cache_control", False),
        (SLIDING_BREAKPOINTS, "$.tools[0].cache_control", False),
        (SLIDING_BREAKPOINTS, "$.messages[2].content[0].text", False),
        ("$.tools[*]", "$.tools[12]", True),
        ("$.tools[*]", "$.tools[0].parameters", False),
        ("$.input*", "$.input[2]", True),
        ("$.input*", "$.instructions", False),
        ("$.input*", "$.input[2].content", False),  # a bare * does not cross a dot
        ("$.input[*].*", "$.input[3].content", True),
        ("$.input[*].*", "$.input[3].content[0].text", False),
        ("$['a.b']", "$['a.b']", True),
        ("$['a.b']", "$.a.b", False),
    ],
)
def test_path_glob(pattern: str, path: str, expected: bool) -> None:
    assert path_matches(path, pattern) is expected


def test_k1_healthy_captures_raise_no_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_system = b'{"model":"gpt-6.1","instructions":"Sys \\u0041","input":"hi"}'
    wire_only = [  # C1: whitespace and escapes only
        _record("w1", {}, parent=None, raw=raw_system, session="w"),
        _record(
            "w2",
            {},
            parent="w1",
            raw=b'{\n  "model": "gpt-6.1",\n  "instructions": "Sys A",\n  "input": "hi"\n}',
            session="w",
        ),
    ]
    healthy = [
        _session([_body(1), _body(2), _body(3)]),  # C2: plain growth
        wire_only,
        [_record("warm", _body(1), parent=None, usage=_usage(cached=1200))],  # C3
    ]

    for records in healthy:
        report = evaluate_diagnoses(analyze_records(records), CheckPolicy.default())
        assert report.errors == ()
        capsys.readouterr()
        assert main(["check", str(_dump(tmp_path, records))]) == 0

    # C1 is still reported, as a warning that does not block
    report = evaluate_diagnoses(analyze_records(wire_only), CheckPolicy.default())
    assert [(f.rule, f.severity) for f in report.findings] == [("wire_only_diff", "warning")]


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_k2_structural_breaks_fail_and_point_at_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    records = _broken(kind)
    rule, json_path = EXPECTED[kind]

    report = evaluate_diagnoses(analyze_records(records), CheckPolicy.default())

    assert [(f.rule, f.json_path) for f in report.errors] == [(rule, json_path)]
    assert report.exit_code == 1
    capsys.readouterr()
    assert main(["check", str(_dump(tmp_path, records))]) == 1
    out = capsys.readouterr().out
    assert rule in out and json_path in out and "turn-0003" in out


def test_k3_sliding_anthropic_breakpoints_are_exempt_only_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records = load_capture_records(FIXTURES / "captured" / "anthropic_messages_turns.jsonl")
    moved = {"$.messages[0].content[0].cache_control", "$.messages[2].content[0].cache_control"}

    strict = evaluate_diagnoses(analyze_records(records), STRICT)
    assert {f.json_path for f in strict.errors} == moved

    exempt = CheckPolicy(
        error=frozenset({"config_changed"}), warning=frozenset(), ignore=frozenset(),
        ignore_paths=(SLIDING_BREAKPOINTS,),
    )
    exempted = evaluate_diagnoses(analyze_records(records), exempt)
    assert exempted.errors == () and exempted.exit_code == 0
    assert {f.severity for f in exempted.findings} == {"ignore"}

    # the pattern is about message blocks: a root-level marker change is not covered by it
    anthropic = EndpointIdentity(
        "anthropic_messages", "anthropic", "claude-sonnet-4-6", "/v1/messages"
    )
    plain = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]}
    root_changed = [
        _record("a1", plain, parent=None, identity=anthropic),
        _record(
            "a2", {"cache_control": {"type": "ephemeral"}, **plain}, parent="a1", identity=anthropic
        ),
    ]
    still_caught = evaluate_diagnoses(analyze_records(root_changed), exempt)
    assert [(f.rule, f.json_path) for f in still_caught.errors] == [
        ("config_changed", "$.cache_control")
    ]

    # the same policy, picked up from a cacherift.toml next to the capture
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cacherift.toml").write_text(
        "[check]\nerror = [\"config_changed\"]\n"
        f"ignore_paths = ['{SLIDING_BREAKPOINTS}']\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert main(["check", str(_dump(tmp_path, records))]) == 0


def test_evaluator_reports_usage_and_lineage_rules_with_line_numbers() -> None:
    records = [
        _record("t1", _body(1), parent=None, usage=_usage(cached=1600, total=1500)),
        _record("t2", _body(2), parent="t1"),
        _record("t3", _body(3), parent="t2", usage={"input_tokens": 10}),
    ]

    default = evaluate_diagnoses(analyze_records(records), CheckPolicy.default())
    assert [(f.rule, f.line) for f in default.errors] == [("usage_invalid", 1)]

    exhaustive = CheckPolicy(
        error=frozenset({"usage_invalid", "usage_missing", "usage_incomplete", "no_baseline"}),
        warning=frozenset(),
    )
    report = evaluate_diagnoses(analyze_records(records), exhaustive)
    assert [(f.rule, f.line) for f in report.errors] == [
        ("usage_invalid", 1),
        ("no_baseline", 1),
        ("usage_missing", 2),
        ("usage_incomplete", 3),
    ]
    assert all(f.record_id == f"t{f.line}" for f in report.findings)


def test_evaluator_does_not_double_report_a_change_and_its_echo_cause() -> None:
    records = _broken("system_prompt_edited")

    report = evaluate_diagnoses(analyze_records(records), CheckPolicy.default())

    assert [(f.rule, f.source) for f in report.errors] == [("mutated_existing", "observed_change")]


def test_evaluator_lists_cause_only_rules_once_per_turn() -> None:
    records = [
        _record("t1", _body(1), parent=None, usage=_usage()),
        _record("t2", _body(2), parent="t1", usage=_usage()),
    ]  # same prefix, no structural change, zero read twice: unexplained_low_hit

    report = evaluate_diagnoses(analyze_records(records), CheckPolicy.default())

    assert [(f.rule, f.severity, f.line) for f in report.findings] == [
        ("unexplained_low_hit", "warning", 2)
    ]
    assert isinstance(report, CheckReport) and report.warnings == report.findings


# --------------------------------------------------------------------------------------
# T3: pytest plugin
# --------------------------------------------------------------------------------------


def test_k5_a_stable_prompt_builder_passes(cacherift_session: PrefixSession) -> None:
    for turn in (1, 2, 3):
        cacherift_session.add(_body(turn))

    report = cacherift_session.assert_stable()

    assert report.errors == () and report.exit_code == 0
    assert len(cacherift_session.records) == 3


def test_k5_a_jittering_prompt_builder_fails_without_leaking_text(
    cacherift_session: PrefixSession,
) -> None:
    secret = "TOP_SECRET_TOOL_SCHEMA_TEXT_424242"
    cacherift_session.add(_body(1, tools=(_tool(),), system=secret))
    cacherift_session.add(_body(2, tools=(_tool(),), system=secret))
    cacherift_session.add(_body(3, tools=(_tool(jitter=True),), system=secret))

    with pytest.raises(AssertionError) as excinfo:
        cacherift_session.assert_stable()

    message = str(excinfo.value)
    assert "reordered_existing" in message and "$.tools[0]" in message and "turn-0003" in message
    assert secret not in message


def test_assert_prefix_stable_takes_records_and_a_policy() -> None:
    records = _broken("tool_key_jitter")

    with pytest.raises(AssertionError):
        assert_prefix_stable(records)
    lenient = CheckPolicy(error=frozenset(), warning=frozenset({"reordered_existing"}))
    assert_prefix_stable(records, policy=lenient)  # downgraded to a warning: no raise
    assert_prefix_stable(_session([_body(1), _body(2)]), policy=ALLOW_EVERYTHING)


def test_session_accepts_raw_wire_bytes_and_builds_one_explicit_session() -> None:
    session = PrefixSession()
    compact = b'{"model":"gpt-6.1","instructions":"Sys \\u0041","input":"hi"}'
    pretty = b'{\n  "model": "gpt-6.1",\n  "instructions": "Sys A",\n  "input": "hi"\n}'

    session.add(compact)
    session.add(pretty)

    first, second = session.records
    assert first.captured_body_bytes == compact and second.captured_body_bytes == pretty
    assert second.client_session_id == first.client_session_id
    assert second.parent_record_id == first.record_id
    assert [(f.rule, f.severity) for f in session.report().findings] == [
        ("wire_only_diff", "warning")
    ]


# --------------------------------------------------------------------------------------
# T4: CLI
# --------------------------------------------------------------------------------------


def test_cli_json_and_github_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capture = _dump(tmp_path, _broken("system_prompt_edited"))

    capsys.readouterr()
    assert main(["check", str(capture), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"errors": 1, "warnings": 0, "ignored": 0}
    assert report["findings"] == [
        {
            "line": 3,
            "record_id": "turn-0003",
            "severity": "error",
            "rule": "mutated_existing",
            "source": "observed_change",
            "json_path": "$.instructions",
            "message": "",
        }
    ]

    assert main(["check", str(capture), "--format", "github"]) == 1
    out = capsys.readouterr().out
    assert out == f"::error file={capture},line=3::turn-0003: mutated_existing $.instructions\n"


def test_cli_github_format_warns_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = _dump(tmp_path, _broken("tool_key_jitter"))
    (tmp_path / "cacherift.toml").write_text('[check]\nwarning = ["reordered_existing"]\n')

    config = str(tmp_path / "cacherift.toml")
    assert main(["check", str(capture), "--format", "github", "--config", config]) == 0
    assert capsys.readouterr().out == (
        f"::warning file={capture},line=3::turn-0003: reordered_existing $.tools[0]\n"
    )


def test_cli_table_format_pass_and_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert main(["check", str(_dump(tmp_path, _session([_body(1), _body(2)])))]) == 0
    passed = capsys.readouterr().out
    assert "error" in passed and "warning" in passed

    assert main(["check", str(_dump(tmp_path, _broken("tool_inserted"), "broken.jsonl"))]) == 1
    failed = capsys.readouterr().out
    assert "inserted_before_tail" in failed and "$.tools[1]" in failed and "turn-0003" in failed


def test_k4_no_output_format_leaks_prompt_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "TOP_SECRET_SYSTEM_PROMPT_ALPHA_98765"
    capture = _dump(tmp_path, _broken("system_prompt_edited", secret=secret))

    for output_format in ("table", "json", "github"):
        capsys.readouterr()
        assert main(["check", str(capture), "--format", output_format]) == 1
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err


def test_cli_configuration_errors_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["check", str(tmp_path / "missing.jsonl")]) == 2
    assert "missing.jsonl" in capsys.readouterr().err

    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json\n", encoding="utf-8")
    assert main(["check", str(broken)]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1

    good = _dump(tmp_path, _session([_body(1)]))
    (tmp_path / "cacherift.toml").write_text('[check]\nerror = ["mutated_existent"]\n')
    assert main(["check", str(good), "--config", str(tmp_path / "cacherift.toml")]) == 2
    assert "mutated_existent" in capsys.readouterr().err

    (tmp_path / "bad.toml").write_text("[check\n", encoding="utf-8")
    assert main(["check", str(good), "--config", str(tmp_path / "bad.toml")]) == 2
    assert capsys.readouterr().err.count("\n") == 1


def test_cli_reads_shipped_captures_as_they_are(capsys: pytest.CaptureFixture[str]) -> None:
    captured = FIXTURES / "captured"

    # the OpenAI sample inserts a tool on its third turn on purpose: a real, blocking break
    assert main(["check", str(captured / "openai_responses_turns.jsonl"), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    errors = [
        (f["rule"], f["json_path"], f["line"])
        for f in report["findings"]
        if f["severity"] == "error"
    ]
    assert errors == [("inserted_before_tail", "$.tools[1]", 3)]

    # the Anthropic sample only slides its breakpoint: reported, never blocking
    anthropic = captured / "anthropic_messages_turns.jsonl"
    assert main(["check", str(anthropic), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"errors": 0, "warnings": 2, "ignored": 0}
