"""The top-level `cacherift` entry point: dispatch, help, and exit codes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cacherift import __version__
from cacherift.cli import DiagnosisSession, main
from cacherift.core.freeze import freeze_json
from cacherift.core.models import CaptureRecord, EndpointIdentity, UsageState
from cacherift.io.capture import dump_capture_record

SUBCOMMANDS = ("analyze", "probe", "proxy", "check", "repair")


@pytest.fixture
def without_optional_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `httpx` and `uvicorn` look absent, as in an analyze-only installation."""
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, package: str | None = None) -> object:
        return None if name in ("httpx", "uvicorn") else real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def test_version_is_printed_without_running_anything(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"cacherift {__version__}\n"


def test_no_command_prints_help_and_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2

    out = capsys.readouterr().out
    for command in SUBCOMMANDS:
        assert command in out


def test_help_lists_every_command_without_optional_dependencies(
    without_optional_dependencies: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for command in SUBCOMMANDS:
        assert command in out


@pytest.mark.parametrize("command", SUBCOMMANDS[1:])
def test_every_subcommand_shows_its_help_without_optional_dependencies(
    without_optional_dependencies: None,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([command, "--help"])

    assert excinfo.value.code == 0
    assert f"cacherift {command}" in capsys.readouterr().out


def test_analyze_keeps_its_v01_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["analyze", str(tmp_path / "missing.jsonl")]) == 1
    assert "missing.jsonl" in capsys.readouterr().err

    capture = tmp_path / "capture.jsonl"
    capture.write_text(
        '{"record_id":"r1","timestamp":1.0,'
        '"identity":{"dialect":"openai_responses","provider":"openai","model":"gpt-6.1",'
        '"endpoint_path":"/v1/responses"},"parsed_body":{"input":"hi"}}\n',
        encoding="utf-8",
    )
    assert main(["analyze", str(capture), "--pricing-table", "no-such-table"]) == 1
    assert "no-such-table" in capsys.readouterr().err
    assert main(["analyze", str(capture)]) == 0

    # a malformed record is a one-line error, like every other subcommand, not a traceback
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"record_id":"r1"}\n', encoding="utf-8")
    assert main(["analyze", str(malformed)]) == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def _record(
    record_id: str, body: dict[str, Any], *, parent: str | None, response: dict[str, Any] | None
) -> CaptureRecord:
    return CaptureRecord(
        record_id=record_id,
        timestamp=100.0,
        identity=EndpointIdentity("openai_responses", "openai", "gpt-6.1", "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json(body),
        response_body=freeze_json(response) if response is not None else None,
        client_session_id="s",
        parent_record_id=parent,
    )


def test_json_report_never_carries_response_bodies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "MODEL_OUTPUT_AND_ERROR_TEXT_424242"
    body = {"model": "gpt-6.1", "input": [{"role": "user", "content": "hi"}]}
    records = [
        _record(
            "r1",
            body,
            parent=None,
            response={"id": "resp_1", "output": [{"text": secret}], "error": {"message": secret}},
        ),
        _record(
            "r2",
            body,
            parent="r1",
            response={"events": [{"type": "response.output_text.delta", "delta": secret}]},
        ),
    ]
    capture = tmp_path / "capture.jsonl"
    capture.write_text(
        "".join(dump_capture_record(record) + "\n" for record in records), encoding="utf-8"
    )

    assert main(["analyze", str(capture), "--format", "json"]) == 0
    assert secret not in capsys.readouterr().out
    assert main(["analyze", str(capture)]) == 0
    assert secret not in capsys.readouterr().out


def test_unparseable_usage_is_invalid_and_does_not_poison_later_turns() -> None:
    def body(turn: int) -> dict[str, Any]:
        history = [{"role": "user", "content": f"q{n}"} for n in range(1, turn)]
        return {"model": "gpt-6.1", "instructions": "Sys", "input": history}

    good = {"input_tokens": 1500, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5}
    records = [
        _record("t1", body(1), parent=None, response={"usage": good}),
        # a gateway that stringified its numbers
        _record(
            "t2",
            body(2),
            parent="t1",
            response={
                "usage": {
                    "input_tokens": "1,234",
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                }
            },
        ),
        _record("t3", body(3), parent="t2", response={"usage": good}),
    ]

    diags = DiagnosisSession().diagnose_all(records)

    assert diags[1].usage.state is UsageState.INVALID
    assert diags[1].usage.notes[0].startswith("usage_not_parseable")
    assert diags[2].lineage.parent_record_id == "t2"  # the session stayed consistent
    assert diags[2].usage.state is UsageState.COMPLETE
