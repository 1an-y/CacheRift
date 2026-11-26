from __future__ import annotations

import pytest

from cacherift.core.freeze import (
    canonical_project_bytes,
    freeze_json,
    order_sensitive_equal,
    sha256_hex,
    thaw_json,
)
from cacherift.core.models import (
    ByteSpan,
    CaptureRecord,
    ChangeKind,
    EndpointIdentity,
    ObservedChange,
    OffsetSpace,
)


def test_freeze_json_is_deeply_immutable_and_preserves_key_order() -> None:
    source = {
        "meta": {"z": 1, "a": 2, "m": 3},
        "tools": [{"name": "search", "args": ["q"]}],
    }
    frozen = freeze_json(source)

    # Mutating original input must not leak into frozen structure
    source["meta"]["z"] = 999  # type: ignore[index]
    source["tools"].append({"name": "extra"})  # type: ignore[attr-defined]

    assert tuple(frozen["meta"].keys()) == ("z", "a", "m")
    assert frozen["meta"]["z"] == 1
    assert len(frozen["tools"]) == 1
    assert isinstance(frozen["tools"], tuple)

    with pytest.raises(TypeError):
        frozen["meta"]["z"] = 2  # type: ignore[index]

    with pytest.raises(TypeError):
        frozen["tools"][0]["name"] = "mutated"  # type: ignore[index]

    assert thaw_json(frozen) == {
        "meta": {"z": 1, "a": 2, "m": 3},
        "tools": [{"name": "search", "args": ["q"]}],
    }


def test_order_sensitive_equal_distinguishes_key_order_and_bool_vs_int() -> None:
    a = freeze_json({"first": 1, "second": 2})
    b = freeze_json({"second": 2, "first": 1})

    # Python MappingProxyType == ignores key order, order_sensitive_equal must not
    assert a == b
    assert not order_sensitive_equal(a, b)

    assert not order_sensitive_equal(freeze_json({"flag": True}), freeze_json({"flag": 1}))
    assert not order_sensitive_equal(freeze_json({"num": 1}), freeze_json({"num": 1.0}))

    c = freeze_json({"arr": [1, {"x": "hi", "y": [True, None]}]})
    d = freeze_json({"arr": [1, {"x": "hi", "y": [True, None]}]})
    assert order_sensitive_equal(c, d)


def test_canonical_project_bytes_preserves_order_by_default() -> None:
    obj_ab = freeze_json({"a": 1, "b": "中"})
    obj_ba = freeze_json({"b": "中", "a": 1})

    bytes_ab = canonical_project_bytes(obj_ab)
    bytes_ba = canonical_project_bytes(obj_ba)

    assert bytes_ab == b'{"a":1,"b":"\xe4\xb8\xad"}'
    assert bytes_ba == b'{"b":"\xe4\xb8\xad","a":1}'
    assert bytes_ab != bytes_ba

    sorted_ab = canonical_project_bytes(obj_ab, sort_keys=True)
    sorted_ba = canonical_project_bytes(obj_ba, sort_keys=True)
    assert sorted_ab == sorted_ba == b'{"a":1,"b":"\xe4\xb8\xad"}'
    assert sha256_hex(b'{"a":1}') == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )


def test_scan_json_spans_exact_utf8_byte_offsets() -> None:
    from cacherift.core.span import scan_json_spans

    raw = (
        b"{\n"
        b'  "model": "gpt-6.1",\n'
        b'  "text": "\\u0041\\"\xe4\xbd\xa0\xe5\xa5\xbd",\n'
        b'  "tools": [{"name": "t1"}, {"name": "t2"}],\n'
        b'  "count": -12.5e+2,\n'
        b'  "ok": false\n'
        b"}"
    )
    spans = scan_json_spans(raw)

    assert spans["$"] == ByteSpan(OffsetSpace.WIRE_RAW, 0, len(raw))
    assert raw[spans["$.model"].start : spans["$.model"].end] == b'"gpt-6.1"'
    assert (
        raw[spans["$.text"].start : spans["$.text"].end] == b'"\\u0041\\"\xe4\xbd\xa0\xe5\xa5\xbd"'
    )
    assert raw[spans["$.tools[0]"].start : spans["$.tools[0]"].end] == b'{"name": "t1"}'
    assert raw[spans["$.tools[1].name"].start : spans["$.tools[1].name"].end] == b'"t2"'
    assert raw[spans["$.count"].start : spans["$.count"].end] == b"-12.5e+2"
    assert raw[spans["$.ok"].start : spans["$.ok"].end] == b"false"


def test_scan_json_spans_projected_space_and_escaped_keys() -> None:
    from cacherift.core.span import scan_json_spans

    raw = b'{"x.y": {"a\\"b": [10, 20]}}'
    spans = scan_json_spans(raw, space=OffsetSpace.PROJECTED)

    assert spans['$["x.y"]'].space is OffsetSpace.PROJECTED
    assert raw[spans['$["x.y"]["a\\"b"][1]'].start : spans['$["x.y"]["a\\"b"][1]'].end] == b"20"


def test_models_frozen_and_default_redaction() -> None:
    identity = EndpointIdentity(
        dialect="openai_responses",
        provider="openai",
        model="gpt-6.1",
        endpoint_path="/v1/responses",
    )
    rec = CaptureRecord(
        record_id="r1",
        timestamp=1700000000.0,
        identity=identity,
        request_headers=freeze_json({"content-type": "application/json"}),
        captured_body_bytes=b"{}",
        parsed_body=freeze_json({}),
    )
    assert rec.response_complete is True
    assert dict(rec.origin_map) == {}

    change = ObservedChange(
        change_kind=ChangeKind.MUTATED_EXISTING,
        json_path="$.instructions",
        span=ByteSpan(OffsetSpace.WIRE_RAW, 10, 20),
        before_len=10,
        after_len=12,
        before_sha256="a" * 64,
        after_sha256="b" * 64,
    )
    assert change.before_snippet is None
    assert change.after_snippet is None


def test_capture_jsonl_preserves_wire_raw_vs_parsed_only(tmp_path: object) -> None:
    import base64
    from pathlib import Path

    from cacherift.io.capture import dump_capture_record, load_capture_records

    assert isinstance(tmp_path, Path)
    raw_bytes = b'{\n  "model": "gpt-6.1",\n  "input": "hello"\n}'
    rec_wire = CaptureRecord(
        record_id="r-wire",
        timestamp=1700000001.0,
        identity=EndpointIdentity(
            dialect="openai_responses",
            provider="openai",
            model="gpt-6.1",
            endpoint_path="/v1/responses",
        ),
        request_headers=freeze_json({"x-request-id": "req-1"}),
        captured_body_bytes=raw_bytes,
        parsed_body=freeze_json({"model": "gpt-6.1", "input": "hello"}),
        response_id="resp_1",
        response_body=freeze_json(
            {"id": "resp_1", "usage": {"input_tokens": 10, "output_tokens": 2}}
        ),
        origin_map=freeze_json({"$.input": "harness:agent.py:12"}),
    )
    rec_parsed_only = CaptureRecord(
        record_id="r-parsed",
        timestamp=1700000002.0,
        identity=rec_wire.identity,
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json({"model": "gpt-6.1", "input": "hello again"}),
    )

    b64_bytes = b'{"model":"gpt-6.1","input":"\xe4\xb8\xad"}'
    b64_line = (
        '{"record_id":"r-b64","timestamp":1700000003.0,'
        '"identity":{"dialect":"openai_responses","provider":"openai",'
        '"model":"gpt-6.1","endpoint_path":"/v1/responses"},'
        '"request_headers":{},'
        f'"raw_body_b64":"{base64.b64encode(b64_bytes).decode("ascii")}"}}'
    )

    jsonl_path = tmp_path / "captures.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                dump_capture_record(rec_wire),
                dump_capture_record(rec_parsed_only),
                b64_line,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = load_capture_records(jsonl_path)
    assert len(loaded) == 3
    assert loaded[0].captured_body_bytes == raw_bytes
    assert loaded[0].origin_map["$.input"] == "harness:agent.py:12"
    assert loaded[1].captured_body_bytes is None
    assert loaded[1].parsed_body["input"] == "hello again"
    assert loaded[2].captured_body_bytes == b64_bytes
    assert loaded[2].parsed_body["input"] == "中"


def test_core_modules_have_no_forbidden_io_imports() -> None:
    import ast
    from pathlib import Path

    forbidden = {"httpx", "requests", "urllib", "socket", "pathlib", "os", "rich"}
    core_dir = Path(__file__).resolve().parents[1] / "src" / "cacherift" / "core"
    for py_file in sorted(core_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    assert top not in forbidden, f"{py_file.name} imports {top}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".", 1)[0]
                assert top not in forbidden, f"{py_file.name} imports {top}"
