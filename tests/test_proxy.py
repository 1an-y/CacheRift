"""v0.2a capture proxy gates X1-X5 (see specs/03-v0.2a-capture-proxy/spec.md)."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import uvicorn

from cacherift.cli import DiagnosisSession, main
from cacherift.core.freeze import freeze_json
from cacherift.core.models import CaptureRecord, EndpointIdentity, TurnDiagnosis, UsageState
from cacherift.dialects.base import get_dialect_adapter
from cacherift.io.capture import load_capture_records
from cacherift.proxy import cli as proxy_cli
from cacherift.proxy.app import (
    ProxyApp,
    detect_dialect,
    detect_provider,
    extract_model,
)
from cacherift.proxy.recorder import CaptureRecorder
from cacherift.proxy.sse import SSEAccumulator

_SSE_HEADERS = {"content-type": "text/event-stream"}
_GEMINI_USAGE = {
    "promptTokenCount": 300,
    "cachedContentTokenCount": 200,
    "candidatesTokenCount": 1,
}
_USAGE: dict[str, Any] = {
    "input_tokens": 1620,
    "input_tokens_details": {"cached_tokens": 1480},
    "output_tokens": 95,
}
_RESPONSE = {"id": "resp_1", "usage": _USAGE}
SESSION_A = {"x-cacherift-session-id": "sess-a"}
SESSION_B = {"x-cacherift-session-id": "sess-b"}
_BODY = '{\n  "model": "gpt-6.1",\n  "input": "A \\u0041 中",\n  "stream": true\n}'.encode()
_SECRET = "sk-SECRET-DO-NOT-LOG-0123456789"


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    proxy_cli.register_parser(parser.add_subparsers())
    return parser.parse_args(["proxy", *argv])


def _openai_stream(cached: int = 1480, *, delta: str = "hello") -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": delta},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "usage": {
                    "input_tokens": 1620,
                    "input_tokens_details": {"cached_tokens": cached},
                    "output_tokens": 95,
                },
            },
        },
    ]


def _anthropic_stream() -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "usage": {
                    "input_tokens": 50,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 1200,
                    "output_tokens": 1,
                },
            },
        },
        {"type": "content_block_delta", "delta": {"text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 15}},
        {"type": "message_delta", "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]


def _frames(*events: dict[str, Any], eol: str = "\n") -> bytes:
    return "".join(
        f"event: {event['type']}{eol}data: {json.dumps(event)}{eol}{eol}" for event in events
    ).encode()


async def _chunks(
    data: bytes, *, size: int = 7, delay: float = 0.0
) -> AsyncGenerator[bytes]:
    for start in range(0, len(data), size):
        if delay:
            await asyncio.sleep(delay)
        yield data[start : start + size]


class _Upstream:
    """httpx.MockTransport handler that records requests and answers however a test scripts."""

    def __init__(
        self, responder: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = responder or _openai_sse

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        await request.aread()
        return await self.responder(request)


async def _openai_sse(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers=_SSE_HEADERS, content=_chunks(_frames(*_openai_stream())))


async def _echo_raw(request: httpx.Request) -> httpx.Response:
    content_type = request.headers.get("content-type", "application/octet-stream")
    return httpx.Response(200, content=request.content, headers={"content-type": content_type})


class _Harness:
    """A proxy app wired to a fake upstream, with no sockets involved."""

    def __init__(
        self,
        tmp_path: Path,
        upstream: _Upstream | None,
        *,
        upstream_url: str = "https://api.openai.com",
        live: bool = False,
        **app_kwargs: Any,
    ) -> None:
        self.upstream = upstream
        self.path = tmp_path / "capture.jsonl"
        self.recorder = CaptureRecorder(
            self.path, live=live, session=DiagnosisSession() if live else None
        )
        self.app = ProxyApp(
            upstream=upstream_url,
            transport=httpx.MockTransport(upstream) if upstream is not None else None,
            recorder=self.recorder,
            **app_kwargs,
        )

    def post(
        self, path: str, *, content: bytes, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://proxy.local"
            ) as client:
                return await client.post(path, content=content, headers=headers or {})

        return asyncio.run(send())

    def records(self) -> list[CaptureRecord]:
        asyncio.run(self.app.aclose())
        self.recorder.close()
        return load_capture_records(self.path)


# --------------------------------------------------------------------------------------
# T1: SSE accumulator
# --------------------------------------------------------------------------------------


def test_sse_parses_events_split_across_arbitrary_chunk_boundaries() -> None:
    raw = _frames(*_openai_stream())
    by_seven = SSEAccumulator()
    for start in range(0, len(raw), 7):
        by_seven.feed(raw[start : start + 7])
    by_one = SSEAccumulator()
    for index in range(len(raw)):
        by_one.feed(raw[index : index + 1])

    expected = [event["type"] for event in _openai_stream()]
    assert [e["type"] for e in by_seven.events] == expected
    assert by_one.events == by_seven.events
    assert by_seven.events[2]["response"]["usage"]["input_tokens_details"]["cached_tokens"] == 1480
    assert by_seven.terminal_seen and by_seven.complete


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_sse_accepts_every_line_terminator(eol: str) -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(_frames(*_anthropic_stream(), eol=eol))
    accumulator.finish()  # the proxy always signals end of stream
    assert [e["type"] for e in accumulator.events] == [
        "message_start",
        "content_block_delta",
        "message_delta",
        "message_delta",
        "message_stop",
    ]
    assert accumulator.complete


def test_sse_joins_multiline_data_and_skips_comments_and_other_fields() -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(
        b": keep-alive\nid: 7\nretry: 100\nevent: ping\ndata: {\ndata: \"x\": 1}\n\n\n"
    )

    assert accumulator.events == [{"type": "ping", "x": 1}]
    assert not accumulator.terminal_seen


def test_sse_keeps_non_json_data_verbatim_and_treats_done_as_terminal() -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(b'data: {"id":"c1","choices":[]}\n\ndata: [DONE]\n\n')

    assert accumulator.events == [{"id": "c1", "choices": []}, {"data": "[DONE]"}]
    assert accumulator.terminal_seen and accumulator.complete


def test_sse_error_event_marks_the_stream_incomplete() -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(_frames({"type": "error", "error": {"message": "overloaded"}}))
    accumulator.feed(_frames({"type": "message_stop"}))

    assert accumulator.error_seen and not accumulator.complete


def test_sse_event_name_fills_in_a_missing_json_type() -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(b"event: message_stop\ndata: {}\n\n")

    assert accumulator.events == [{"type": "message_stop"}] and accumulator.terminal_seen


def test_sse_unfinished_trailing_frame_is_never_dispatched() -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(b'data: {"type": "message_stop"}\n')  # the blank line never arrived
    accumulator.finish()

    assert accumulator.events == [] and not accumulator.terminal_seen


# --------------------------------------------------------------------------------------
# T2: recorder
# --------------------------------------------------------------------------------------


def _record(record_id: str, *, number: int) -> CaptureRecord:
    body = {"n": number}
    return CaptureRecord(
        record_id=record_id,
        timestamp=1000.0,
        identity=EndpointIdentity("openai_responses", "openai", "gpt-6.1", "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=json.dumps(body).encode(),
        parsed_body=body,
        response_body=_RESPONSE,
    )


def test_recorder_serializes_concurrent_writes_into_parseable_lines(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"

    async def scenario() -> list[str]:
        recorder = CaptureRecorder(path)

        async def writer(number: int) -> str:
            record_id = recorder.next_record_id()
            await asyncio.sleep(0)
            await recorder.write(_record(record_id, number=number))
            return record_id

        ids = await asyncio.gather(*(writer(n) for n in range(40)))
        recorder.close()
        return ids

    ids = asyncio.run(scenario())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 40
    records = load_capture_records(path)
    assert sorted(r.parsed_body["n"] for r in records) == list(range(40))
    assert len(set(ids)) == 40
    assert {r.record_id for r in records} == set(ids)


def test_recorder_appends_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    for run in (1, 2):
        recorder = CaptureRecorder(path)
        asyncio.run(recorder.write(_record(recorder.next_record_id(), number=run)))
        recorder.close()

    assert [r.parsed_body["n"] for r in load_capture_records(path)] == [1, 2]


def test_live_recorder_prints_one_bodied_free_line_per_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = CaptureRecorder(tmp_path / "capture.jsonl", live=True, session=DiagnosisSession())

    body = b'{"model":"gpt-6.1","instructions":"TOP_SECRET_SYSTEM_PROMPT","input":"hi"}'
    record = CaptureRecord(
        record_id="cap-x-00001",
        timestamp=1000.0,
        identity=EndpointIdentity("openai_responses", "openai", "gpt-6.1", "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=body,
        parsed_body=json.loads(body),
        response_body=_RESPONSE,
    )
    asyncio.run(recorder.write(record))
    recorder.close()

    captured = capsys.readouterr()
    assert "TOP_SECRET_SYSTEM_PROMPT" not in captured.err + captured.out
    assert "cap-x-00001" in captured.err
    assert "read=1480" in captured.err and "no_baseline" in captured.err
    assert captured.out == ""


# --------------------------------------------------------------------------------------
# T3: ASGI proxy
# --------------------------------------------------------------------------------------


def test_x1_request_and_response_bytes_pass_through_unchanged(tmp_path: Path) -> None:
    upstream = _Upstream()
    harness = _Harness(tmp_path, upstream)

    response = harness.post("/v1/responses", content=_BODY, headers={"authorization": "Bearer x"})

    stream = _frames(*_openai_stream())
    assert response.status_code == 200
    assert response.content == stream  # what the client got is what the upstream sent
    assert upstream.requests[0].content == _BODY  # what the upstream got is what the client sent
    (record,) = harness.records()
    assert record.captured_body_bytes == _BODY
    assert record.parsed_body["input"] == "A A 中"
    assert record.identity.dialect == "openai_responses"
    assert record.identity.model == "gpt-6.1"
    assert record.identity.endpoint_path == "/v1/responses"


def test_request_headers_are_forwarded_except_hop_by_hop_and_proxy_own(tmp_path: Path) -> None:
    upstream = _Upstream()
    harness = _Harness(tmp_path, upstream)

    harness.post(
        "/v1/responses",
        content=_BODY,
        headers={
            "authorization": "Bearer x",
            "x-request-id": "req-1",
            "accept": "text/event-stream",
            "accept-encoding": "gzip, br",
            "connection": "close",
            "transfer-encoding": "chunked",
            "expect": "100-continue",
            "x-cacherift-session-id": "sess-7",
        },
    )

    sent = upstream.requests[0].headers
    assert sent["authorization"] == "Bearer x"
    assert sent["x-request-id"] == "req-1"
    assert sent["accept"] == "text/event-stream"
    assert sent["host"] == "api.openai.com"
    assert sent["accept-encoding"] == "identity"
    assert sent["content-length"] == str(len(_BODY))
    # httpx sets its own connection header on the upstream leg; the client's value must not win
    assert sent.get("connection") != "close"
    for name in ("transfer-encoding", "expect", "x-cacherift-session-id"):
        assert name not in sent
    (record,) = harness.records()
    assert record.client_session_id == "sess-7"
    assert record.request_headers["authorization"] == "[REDACTED]"
    assert record.request_headers["accept-encoding"] == "gzip, br"  # what the client sent
    assert record.request_headers["x-cacherift-session-id"] == "sess-7"


@pytest.mark.parametrize(
    ("path", "header", "value"),
    [
        ("/v1/responses", "authorization", f"Bearer {_SECRET}"),
        ("/v1/messages", "x-api-key", _SECRET),
    ],
)
def test_x2_credentials_reach_upstream_but_never_the_capture(
    tmp_path: Path, path: str, header: str, value: str
) -> None:
    async def echo_credential(request: httpx.Request) -> httpx.Response:
        events = _openai_stream(delta=f"echo {request.headers[header]}")
        return httpx.Response(200, headers=_SSE_HEADERS, content=_chunks(_frames(*events)))

    upstream = _Upstream(echo_credential)
    harness = _Harness(tmp_path, upstream)

    harness.post(path, content=_BODY, headers={header: value})

    assert upstream.requests[0].headers[header] == value  # forwarded intact
    (record,) = harness.records()
    assert record.request_headers[header] == "[REDACTED]"
    assert record.response_body["events"][1]["delta"] == "echo [REDACTED]"
    assert _SECRET not in harness.path.read_text(encoding="utf-8")


def test_x2_query_string_secrets_are_forwarded_but_never_recorded(tmp_path: Path) -> None:
    upstream = _Upstream(_echo_raw)
    harness = _Harness(
        tmp_path, upstream, upstream_url="https://generativelanguage.googleapis.com"
    )

    harness.post(
        "/v1beta/models/gemini-2.5-pro:generateContent?key=AIza-SECRET",
        content=b'{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}',
    )

    assert upstream.requests[0].url.query == b"key=AIza-SECRET"  # forwarded intact
    (record,) = harness.records()
    assert record.identity.dialect == "gemini_generate_content"
    assert record.identity.provider == "gemini"
    assert record.identity.model == "gemini-2.5-pro"
    assert record.identity.endpoint_path == "/v1beta/models/gemini-2.5-pro:generateContent"
    assert "AIza-SECRET" not in harness.path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "events", "dialect", "response_id", "cache_read", "output"),
    [
        ("/v1/responses", _openai_stream(), "openai_responses", "resp_1", 1480, 95),
        ("/v1/messages", _anthropic_stream(), "anthropic_messages", "msg_1", 1200, 42),
    ],
)
def test_x3_complete_sse_streams_yield_usage(
    tmp_path: Path,
    path: str,
    events: list[dict[str, Any]],
    dialect: str,
    response_id: str,
    cache_read: int,
    output: int,
) -> None:
    async def stream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_SSE_HEADERS, content=_chunks(_frames(*events)))

    harness = _Harness(tmp_path, _Upstream(stream))

    response = harness.post(path, content=_BODY)

    assert response.status_code == 200
    (record,) = harness.records()
    assert record.response_complete is True
    assert record.response_id == response_id
    assert [e["type"] for e in record.response_body["events"]] == [e["type"] for e in events]
    usage = get_dialect_adapter(dialect).extract_usage(record)
    assert usage.state is UsageState.COMPLETE
    assert usage.buckets is not None
    assert (usage.buckets.cache_read, usage.buckets.output) == (cache_read, output)


def test_x3_interrupted_stream_is_incomplete_and_aborts_the_client(tmp_path: Path) -> None:
    async def cut(request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncGenerator[bytes]:
            yield _frames(_openai_stream()[0])
            yield b'data: {"type": "response.output_text.delta", "delta": "hel"'
            raise httpx.ReadError("upstream cut the stream")

        return httpx.Response(200, headers=_SSE_HEADERS, content=body())

    harness = _Harness(tmp_path, _Upstream(cut))

    # The proxy returns without the final body message, which is how an ASGI app aborts a
    # connection. httpx's in-process transport cannot represent that and refuses the response;
    # the real-server test below shows the client socket actually being dropped.
    with pytest.raises(AssertionError):
        harness.post("/v1/responses", content=_BODY)

    (record,) = harness.records()
    assert record.response_complete is False
    assert [e["type"] for e in record.response_body["events"]] == ["response.created"]
    usage = get_dialect_adapter("openai_responses").extract_usage(record)
    assert usage.state is UsageState.MISSING and usage.buckets is None


def test_x3_stream_without_a_terminal_frame_is_incomplete(tmp_path: Path) -> None:
    async def truncated(request: httpx.Request) -> httpx.Response:
        frames = _frames(*_openai_stream()[:-1])  # ends cleanly, but response.completed never came
        return httpx.Response(200, headers=_SSE_HEADERS, content=_chunks(frames))

    harness = _Harness(tmp_path, _Upstream(truncated))

    response = harness.post("/v1/responses", content=_BODY)

    assert response.status_code == 200
    (record,) = harness.records()
    assert record.response_complete is False
    assert len(record.response_body["events"]) == 2
    usage = get_dialect_adapter("openai_responses").extract_usage(record)
    assert usage.state is UsageState.MISSING and usage.buckets is None


def test_x4_concurrent_sessions_do_not_cross_or_corrupt_the_capture(tmp_path: Path) -> None:
    async def slow_echo(request: httpx.Request) -> httpx.Response:
        marker = json.loads(request.content)["user"]
        events = _openai_stream(delta=marker)
        frames = _frames(*events)
        return httpx.Response(200, headers=_SSE_HEADERS, content=_chunks(frames, delay=0.01))

    upstream = _Upstream(slow_echo)
    harness = _Harness(tmp_path, upstream)

    def body(marker: str) -> bytes:
        return json.dumps({"model": "gpt-6.1", "user": marker, "input": "x"}).encode()

    async def two_clients() -> tuple[str, str]:
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy.local") as client:
            first, second = await asyncio.gather(
                client.post("/v1/responses", content=body("alpha"), headers=SESSION_A),
                client.post("/v1/responses", content=body("bravo"), headers=SESSION_B),
            )
        return first.text, second.text

    first, second = asyncio.run(two_clients())

    assert "alpha" in first and "bravo" not in first
    assert "bravo" in second and "alpha" not in second
    records = harness.records()
    by_session = {r.client_session_id: r for r in records}
    assert set(by_session) == {"sess-a", "sess-b"}
    assert by_session["sess-a"].parsed_body["user"] == "alpha"
    assert by_session["sess-b"].parsed_body["user"] == "bravo"
    sent_bodies = {request.content for request in upstream.requests}
    assert {r.captured_body_bytes for r in records} == sent_bodies == {body("alpha"), body("bravo")}


def test_x5_upstream_errors_pass_through_and_the_proxy_stays_alive(tmp_path: Path) -> None:
    calls = 0

    async def limited_then_ok(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "30", "connection": "close"},
                json={"error": {"type": "rate_limit_error", "message": "slow down"}},
            )
        return await _openai_sse(request)

    harness = _Harness(tmp_path, _Upstream(limited_then_ok))

    limited = harness.post("/v1/responses", content=_BODY)
    ok = harness.post("/v1/responses", content=_BODY)

    assert limited.status_code == 429
    assert limited.json() == {"error": {"type": "rate_limit_error", "message": "slow down"}}
    assert limited.headers["retry-after"] == "30"
    assert "connection" not in limited.headers
    assert ok.status_code == 200
    first, second = harness.records()
    assert first.response_complete is False
    assert first.response_body == {"error": {"type": "rate_limit_error", "message": "slow down"}}
    assert second.response_complete is True


def test_x5_unreachable_upstream_returns_502_and_records_an_incomplete_request(
    tmp_path: Path,
) -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused by 10.0.0.7:443")

    harness = _Harness(tmp_path, _Upstream(refuse))

    response = harness.post("/v1/responses", content=_BODY)

    assert response.status_code == 502
    assert response.json() == {
        "error": {"type": "cacherift_upstream_unreachable", "message": "ConnectError"}
    }
    assert "10.0.0.7" not in response.text  # upstream internals stay out of the client response
    (record,) = harness.records()
    assert record.response_complete is False and record.response_body is None
    assert record.captured_body_bytes == _BODY


def test_non_streaming_json_response_is_recorded_with_its_response_id(tmp_path: Path) -> None:
    payload = {
        "id": "resp_9",
        "status": "completed",
        "usage": _USAGE,
    }

    async def plain_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    harness = _Harness(tmp_path, _Upstream(plain_json))

    response = harness.post("/v1/responses", content=b'{"model":"gpt-6.1","input":"hi"}')

    assert response.json() == payload
    (record,) = harness.records()
    assert record.response_complete is True
    assert record.response_id == "resp_9"
    assert record.response_body["id"] == "resp_9"


@pytest.mark.parametrize(
    ("host", "path", "dialect", "provider", "path_model"),
    [
        ("api.openai.com", "/v1/responses", "openai_responses", "openai", None),
        ("api.anthropic.com", "/v1/messages", "anthropic_messages", "anthropic", None),
        ("api.openai.com", "/v1/chat/completions", "openai_chat", "openai", None),
        ("api.deepseek.com", "/chat/completions", "deepseek_chat", "deepseek", None),
        (
            "generativelanguage.googleapis.com",
            "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
            "gemini_generate_content",
            "gemini",
            "gemini-2.5-pro",
        ),
        ("gw.corp.internal", "/v1/responses", "openai_responses", "unknown", None),
        ("api.openai.com", "/v1/embeddings", None, "openai", None),
    ],
)
def test_identity_comes_from_the_request_path_and_upstream_host(
    host: str, path: str, dialect: str | None, provider: str, path_model: str | None
) -> None:
    assert detect_dialect(path, host) == dialect
    assert detect_provider(host) == provider
    assert extract_model({"model": "m"}, path) == "m"
    assert extract_model({}, path) == (path_model or "unknown")


def test_non_json_bodies_and_unrecognized_paths_are_proxied_but_not_recorded(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, _Upstream(_echo_raw))

    first = harness.post("/v1/responses", content=b"not json at all")
    second = harness.post("/v1/models", content=b'{"q": 1}')

    assert first.status_code == second.status_code == 200
    assert harness.records() == []


def test_dialect_override_records_unrecognized_paths(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path, _Upstream(_echo_raw), dialect="openai_chat", pricing_table_id="default-2026-11"
    )

    harness.post("/custom/chat", content=b'{"model":"m","messages":[]}')

    (record,) = harness.records()
    assert record.identity.dialect == "openai_chat"
    assert record.identity.endpoint_path == "/custom/chat"
    assert record.identity.pricing_table_id == "default-2026-11"
    assert record.client_session_id is None  # no header, no forced grouping


def test_client_disconnect_closes_the_upstream_stream(tmp_path: Path) -> None:
    closed = False

    async def slow(request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncGenerator[bytes]:
            nonlocal closed
            try:
                yield _frames(_openai_stream()[0])
                await asyncio.sleep(0.05)
                yield _frames(*_openai_stream()[1:])
            finally:
                closed = True

        return httpx.Response(200, headers=_SSE_HEADERS, content=body())

    harness = _Harness(tmp_path, _Upstream(slow))
    delivered_request = False
    first_chunk_sent: asyncio.Event | None = None

    async def receive() -> dict[str, Any]:
        nonlocal delivered_request
        if not delivered_request:
            delivered_request = True
            return {"type": "http.request", "body": _BODY, "more_body": False}
        # A real client walks away while the stream is running, not the instant it finished
        # uploading: hold the disconnect until the proxy has forwarded its first chunk.
        assert first_chunk_sent is not None
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}

    async def scenario() -> list[dict[str, Any]]:
        nonlocal first_chunk_sent
        first_chunk_sent = asyncio.Event()
        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_chunk_sent.set()

        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "query_string": b"",
            "headers": [(b"host", b"proxy.local"), (b"content-type", b"application/json")],
        }
        await harness.app(cast(Any, scope), cast(Any, receive), cast(Any, send))
        return messages

    messages = asyncio.run(scenario())

    assert closed is True
    assert messages[0]["type"] == "http.response.start"
    (record,) = harness.records()
    assert record.response_complete is False


def _drive(app: ProxyApp, receive: Callable[[], Awaitable[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Call the ASGI app directly with a scripted ``receive``; returns what it sent."""
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def scenario() -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "query_string": b"",
            "headers": [(b"host", b"proxy.local"), (b"content-type", b"application/json")],
        }
        await app(cast(Any, scope), cast(Any, receive), cast(Any, send))

    asyncio.run(scenario())
    return messages


def test_client_leaving_mid_upload_forwards_nothing(tmp_path: Path) -> None:
    upstream = _Upstream()
    harness = _Harness(tmp_path, upstream)
    uploads: Iterator[dict[str, Any]] = iter(
        [
            {"type": "http.request", "body": _BODY[:12], "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict[str, Any]:
        return next(uploads)

    assert _drive(harness.app, receive) == []

    assert upstream.requests == []
    assert harness.records() == []


def test_client_leaving_while_the_upstream_thinks_cancels_the_upstream_request(
    tmp_path: Path,
) -> None:
    answering = False

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal answering
        answering = True
        try:
            await asyncio.sleep(5)  # a model that takes a long time to produce its first byte
        except asyncio.CancelledError:
            answering = False  # nothing waits for this answer any more
            raise
        return await _openai_sse(request)

    harness = _Harness(tmp_path, _Upstream(slow))
    uploads: Iterator[dict[str, Any]] = iter(
        [
            {"type": "http.request", "body": _BODY, "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict[str, Any]:
        message = next(uploads)
        if message["type"] == "http.disconnect":
            assert answering  # the upstream had started working on it
        return message

    started = time.monotonic()
    assert _drive(harness.app, receive) == []

    assert time.monotonic() - started < 1  # it did not sit out the upstream's five seconds
    assert answering is False
    (record,) = harness.records()
    assert record.response_complete is False and record.captured_body_bytes == _BODY


@pytest.mark.parametrize("broken", ["disk", "diagnosis"])
def test_capture_failures_never_break_a_delivered_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broken: str,
) -> None:
    def failing_write(line: str) -> None:
        raise OSError(28, "No space left on device")

    def failing_feed(self: DiagnosisSession, record: CaptureRecord) -> TurnDiagnosis:
        raise ValueError("odd provider data")

    harness = _Harness(tmp_path, _Upstream(), live=True)
    if broken == "disk":
        monkeypatch.setattr(harness.recorder._fh, "write", failing_write)
    else:
        monkeypatch.setattr(DiagnosisSession, "feed", failing_feed)

    response = harness.post("/v1/responses", content=_BODY)

    assert response.status_code == 200
    assert response.content == _frames(*_openai_stream())  # complete, not truncated
    err = capsys.readouterr().err
    assert f"cannot {'capture' if broken == 'disk' else 'diagnose'}" in err
    harness.recorder.close()


def test_chat_style_error_frame_marks_the_stream_incomplete() -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(b'data: {"error": {"message": "overloaded", "type": "server_error"}}\n\n')
    accumulator.feed(b"data: [DONE]\n\n")

    assert accumulator.terminal_seen and accumulator.error_seen and not accumulator.complete


def test_gemini_streams_end_with_the_connection_rather_than_a_terminal_frame(
    tmp_path: Path,
) -> None:
    async def stream(request: httpx.Request) -> httpx.Response:
        part = {"content": {"parts": [{"text": "hi"}]}}
        chunk = {"candidates": [part], "usageMetadata": _GEMINI_USAGE}
        frames = f"data: {json.dumps(chunk)}\n\n".encode()
        return httpx.Response(200, headers=_SSE_HEADERS, content=_chunks(frames))

    harness = _Harness(
        tmp_path, _Upstream(stream), upstream_url="https://generativelanguage.googleapis.com"
    )

    harness.post("/v1beta/models/gemini-2.5-pro:streamGenerateContent", content=b'{"contents":[]}')

    (record,) = harness.records()
    assert record.response_complete is True


# --------------------------------------------------------------------------------------
# T3b: the same app under a real uvicorn server and a raw HTTP/1.1 upstream
# --------------------------------------------------------------------------------------


class _RawUpstream:
    """Minimal chunked HTTP/1.1 responder: slow SSE that can cut itself off or be cancelled."""

    def __init__(self, *, cut: bool = False) -> None:
        self.cut = cut
        self.cancelled = False

    async def __call__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def chunk(data: bytes) -> bytes:
            return f"{len(data):X}\r\n".encode() + data + b"\r\n"

        events = _openai_stream()
        parts = [_frames(events[0])] if self.cut else [_frames(event) for event in events]
        try:
            # Consume the request first: closing a socket with unread data on it sends a TCP
            # reset, which can overtake the response and turn a mid-stream cut into a 502.
            head = await reader.readuntil(b"\r\n\r\n")
            lengths = [
                line.split(b":", 1)[1]
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ]
            await reader.readexactly(int(lengths[0]) if lengths else 0)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            await writer.drain()
            for part in parts:
                if reader.at_eof():
                    self.cancelled = True  # the proxy closed its side: stop generating
                    return
                writer.write(chunk(part))
                await writer.drain()
                if self.cut:
                    writer.close()
                    return
                await asyncio.sleep(0.02)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except ConnectionError:
            self.cancelled = True  # the proxy closed the connection under us
        finally:
            writer.close()


def _start_uvicorn(app: ProxyApp) -> tuple[uvicorn.Server, threading.Thread, int]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    listen_socket = server.servers[0].sockets[0]
    return server, thread, listen_socket.getsockname()[1]


def _run_against_real_server(tmp_path: Path, raw: _RawUpstream, client_scenario: Any) -> bool:
    async def scenario() -> bool:
        listening = await asyncio.start_server(raw, "127.0.0.1", 0)
        upstream_port = listening.sockets[0].getsockname()[1]
        harness = _Harness(tmp_path, None, upstream_url=f"http://127.0.0.1:{upstream_port}")
        server, thread, port = _start_uvicorn(harness.app)
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=10
            ) as client:
                outcome = await client_scenario(client, raw)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            listening.close()
            await listening.wait_closed()
        harness.recorder.close()
        assert load_capture_records(harness.path)[0].response_complete is False
        return bool(outcome)

    return asyncio.run(scenario())


def test_real_server_drops_the_client_connection_when_the_upstream_stream_dies(
    tmp_path: Path,
) -> None:
    async def read_everything(client: httpx.AsyncClient, raw: _RawUpstream) -> bool:
        try:
            response = await client.post("/v1/responses", content=_BODY)
        except httpx.RemoteProtocolError:
            return True  # the socket was dropped instead of a clean end of stream
        raise AssertionError(f"client got a well-formed response: {response.status_code}")

    assert _run_against_real_server(tmp_path, _RawUpstream(cut=True), read_everything)


def test_real_server_closes_the_upstream_stream_when_the_client_disconnects(
    tmp_path: Path,
) -> None:
    async def leave_mid_stream(client: httpx.AsyncClient, raw: _RawUpstream) -> bool:
        async with client.stream("POST", "/v1/responses", content=_BODY) as response:
            first = await response.aiter_bytes().__anext__()
            assert first  # the stream had started when the client walked away
        for _ in range(300):
            if raw.cancelled:
                return True
            await asyncio.sleep(0.01)
        return False

    assert _run_against_real_server(tmp_path, _RawUpstream(), leave_mid_stream)


# --------------------------------------------------------------------------------------
# T4: `cacherift proxy` CLI
# --------------------------------------------------------------------------------------


def test_cli_starts_uvicorn_on_the_loopback_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    output = tmp_path / "session.jsonl"
    rc = proxy_cli.run_command(
        _args("--upstream", "https://api.openai.com", "--output", str(output), "--live")
    )

    assert rc == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8787
    assert seen["app"].upstream == "https://api.openai.com"
    err = capsys.readouterr().err
    assert "http://127.0.0.1:8787" in err and str(output) in err
    assert seen["app"].recorder.closed is True


def test_cli_rejects_invalid_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases = [
        (["--upstream", "https://user:pw@api.openai.com"], "userinfo"),
        (["--upstream", "https://api.openai.com?key=abc"], "query"),
        (["--upstream", "api.openai.com"], "scheme"),
        (["--listen", "8787"], "--listen"),
        (["--listen", "127.0.0.1:notaport"], "port"),
        (["--upstream", "https://api.openai.com:abc"], "port"),
    ]
    for extra, fragment in cases:
        rc = proxy_cli.run_command(
            _args("--upstream", "https://api.openai.com", "--output", "x.jsonl", *extra)
        )
        err = capsys.readouterr().err
        assert rc == 2, extra
        assert fragment in err, extra
    # an unknown dialect name is a usage error: argparse exits with the same code
    with pytest.raises(SystemExit) as excinfo:
        _args("--upstream", "https://api.openai.com", "--dialect", "klingon")
    assert excinfo.value.code == 2


def test_proxy_without_optional_dependencies_prints_one_line_hint_and_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, package: str | None = None) -> Any:
        return None if name in ("httpx", "uvicorn") else real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    rc = main(["proxy", "--upstream", "https://api.openai.com", "--output", "x.jsonl"])

    err = capsys.readouterr().err
    assert rc == 2
    assert err.count("\n") == 1 and "cacherift[proxy]" in err


