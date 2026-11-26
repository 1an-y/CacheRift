from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest

from cacherift.capabilities.registry import CapabilityRegistry
from cacherift.cli import DiagnosisSession, main
from cacherift.core.freeze import freeze_json, thaw_json
from cacherift.core.models import (
    CacheWriteBucket,
    CaptureRecord,
    ChangeKind,
    DisplayRole,
    EndpointIdentity,
    LineageStatus,
    OffsetSpace,
    TokenBuckets,
    UsageObservation,
    UsageState,
)
from cacherift.dialects.base import get_dialect_adapter
from cacherift.io.capture import dump_capture_record, load_capture_records
from stream_fixtures import (
    CHAT,
    CHAT_DONE,
    CHAT_ERROR,
    CHAT_FINAL_USAGE,
    DEEPSEEK,
    DEEPSEEK_FINAL_USAGE,
    GEMINI,
    GEMINI_ERROR,
    MODEL_TEXT,
    capture,
    chat_chunk,
    chat_final,
    data_frames,
    deepseek_chunk,
    gemini_frame,
    gemini_meta,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_capability_registry_known_vs_unknown_and_provider_decoupling() -> None:
    reg = CapabilityRegistry.load_default()

    cap_61 = reg.lookup("openai_responses", "openai", "gpt-6.1")
    assert cap_61.support_level == "full"
    assert cap_61.min_cache_tokens == 1024
    assert cap_61.unit_order == ("tools", "instructions", "input")

    cap_62 = reg.lookup("openai_responses", "openai", "gpt-6.2-astra")
    assert cap_62.support_level == "full"
    assert cap_62.unit_order == ("tools", "input", "instructions")
    assert cap_62.prefix_order_note is not None
    assert "instructions" in cap_62.prefix_order_note

    # Dialect alone must not inherit OpenAI rules when provider/model is custom/unknown
    cap_foreign = reg.lookup("openai_responses", "vllm-custom", "my-private-model")
    assert cap_foreign.support_level == "unknown"
    assert cap_foreign.min_cache_tokens is None
    assert cap_foreign.ttl_seconds_hint is None
    assert cap_foreign.unit_order == ()

    # Anthropic models follow vendor documented minimum cacheable prompt lengths
    for model, expected_floor in (
        ("claude-opus-4-6", 4096),
        ("claude-opus-4-5", 4096),
        ("claude-haiku-4-5", 4096),
        ("claude-opus-4-1", 1024),
        ("claude-sonnet-4-6", 1024),
    ):
        cap_ant = reg.lookup("anthropic_messages", "anthropic", model)
        assert cap_ant.min_cache_tokens == expected_floor


def test_openai_responses_units_wire_spans_diagnostics_and_gpt62_order() -> None:
    records = load_capture_records(FIXTURES_DIR / "captured" / "openai_responses_turns.jsonl")
    reg = CapabilityRegistry.load_default()
    adapter = get_dialect_adapter("openai_responses")

    turn2 = records[1]
    cap_61 = reg.lookup(turn2.identity.dialect, turn2.identity.provider, turn2.identity.model)
    units, cfg = adapter.extract_ordered_units(turn2, cap_61)

    assert [u.json_path for u in units] == [
        "$.tools[0]",
        "$.instructions",
        "$.input[0]",
        "$.input[1]",
        "$.input[2]",
    ]
    assert units[0].unit_id == "tool:read_file"
    assert units[0].wire_span is not None
    assert units[0].wire_span.space is OffsetSpace.WIRE_RAW
    assert turn2.captured_body_bytes is not None
    assert (
        b"read_file" in turn2.captured_body_bytes[units[0].wire_span.start : units[0].wire_span.end]
    )

    # previous_response_id and comparison_response_id are linkage refs, not cache-breaking params
    assert adapter.extract_previous_response_ref(turn2) == "resp_001"
    assert ("$.prompt_cache_key", "prompt_cache_key", "shared-team-key") in cfg.cache_params
    assert not any("comparison_response_id" in name for _, name, _ in cfg.cache_params)

    usage = adapter.extract_usage(turn2)
    assert usage.state is UsageState.COMPLETE
    assert usage.buckets is not None
    assert usage.buckets.total_input == 1620
    assert usage.buckets.cache_read == 1480
    assert usage.buckets.uncached_input == 140
    assert usage.buckets.output == 95

    diag = adapter.extract_provider_diagnostics(records[2])
    assert diag is not None
    assert diag.source == "openai_prompt_cache_diagnostics"
    assert diag.compared_response_id == "resp_002"
    assert "tools" in diag.reported_divergences or "tools_changed" in diag.reported_divergences

    # GPT-6.2 places top-level instructions after input
    cap_62 = reg.lookup("openai_responses", "openai", "gpt-6.2")
    gpt62_rec = CaptureRecord(
        record_id="g62-1",
        timestamp=1.0,
        identity=EndpointIdentity("openai_responses", "openai", "gpt-6.2", "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json(
            {
                "model": "gpt-6.2",
                "tools": [{"type": "function", "name": "ping"}],
                "instructions": "Be concise.",
                "input": [{"role": "user", "content": "hi"}],
            }
        ),
    )
    units_62, _ = adapter.extract_ordered_units(gpt62_rec, cap_62)
    assert [u.json_path for u in units_62] == ["$.tools[0]", "$.input[0]", "$.instructions"]
    assert all(u.wire_span is None for u in units_62)


def test_anthropic_messages_denominator_breakpoints_and_streaming_c6() -> None:
    records = load_capture_records(FIXTURES_DIR / "captured" / "anthropic_messages_turns.jsonl")
    reg = CapabilityRegistry.load_default()
    adapter = get_dialect_adapter("anthropic_messages")

    t1, t2 = records[0], records[1]
    cap = reg.lookup(t1.identity.dialect, t1.identity.provider, t1.identity.model)

    units_1, cfg_1 = adapter.extract_ordered_units(t1, cap)
    units_2, cfg_2 = adapter.extract_ordered_units(t2, cap)

    assert [u.display_role for u in units_1] == [
        DisplayRole.TOOLS,
        DisplayRole.SYSTEM_OR_INSTRUCTIONS,
        DisplayRole.CURRENT_INPUT,
    ]
    # Sliding cache_control must go to cache_params without mutating projected message content
    assert units_1[2].projected_bytes == units_2[2].projected_bytes
    assert any(
        path == "$.messages[0].content[0].cache_control" for path, _, _ in cfg_1.cache_params
    )
    assert any(
        path == "$.messages[2].content[0].cache_control" for path, _, _ in cfg_2.cache_params
    )

    # Anthropic denominator = uncached + cache_read + sum(writes), no double-counting
    usage_1 = adapter.extract_usage(t1)
    assert usage_1.state is UsageState.COMPLETE
    assert usage_1.buckets is not None
    assert usage_1.buckets.uncached_input == 20
    assert usage_1.buckets.cache_read == 0
    assert [(w.tier, w.tokens) for w in usage_1.buckets.cache_writes] == [("5m", 1800)]
    assert usage_1.buckets.total_input == 1820

    usage_2 = adapter.extract_usage(t2)
    assert usage_2.state is UsageState.COMPLETE
    assert usage_2.buckets is not None
    assert usage_2.buckets.total_input == 120 + 1800 + 80
    assert adapter.extract_previous_response_ref(t2) == "msg_001"

    diag = adapter.extract_provider_diagnostics(t2)
    assert diag is not None
    assert diag.source == "anthropic_diagnostics"
    assert diag.caveat is not None

    # C6: interrupted SSE stream -> MISSING; cumulative message_delta -> final snapshot only
    edge_records = load_capture_records(FIXTURES_DIR / "synthetic" / "dialects_edge_cases.jsonl")
    interrupted = adapter.extract_usage(edge_records[0])
    assert interrupted.state is UsageState.MISSING
    assert interrupted.buckets is None

    cumulative = adapter.extract_usage(edge_records[1])
    assert cumulative.state is UsageState.COMPLETE
    assert cumulative.buckets is not None
    assert cumulative.buckets.output == 42
    assert cumulative.buckets.total_input == 1250

    # Contradictory cache_creation_input_tokens vs tier sum -> INVALID
    contradictory = CaptureRecord(
        record_id="ant-bad",
        timestamp=1.0,
        identity=t1.identity,
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json({"model": "claude-sonnet-4-6", "messages": []}),
        response_body=freeze_json(
            {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 999,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 100,
                        "ephemeral_1h_input_tokens": 0,
                    },
                    "output_tokens": 5,
                }
            }
        ),
    )
    assert adapter.extract_usage(contradictory).state is UsageState.INVALID


def test_partial_usage_only_dialects() -> None:
    ds_adapter = get_dialect_adapter("deepseek_chat")
    ds_rec = CaptureRecord(
        record_id="ds-1",
        timestamp=1.0,
        identity=EndpointIdentity(
            "deepseek_chat", "deepseek", "deepseek-chat", "/v1/chat/completions"
        ),
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json(
            {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        ),
        response_body=freeze_json(
            {
                "usage": {
                    "prompt_tokens": 500,
                    "prompt_cache_hit_tokens": 400,
                    "prompt_cache_miss_tokens": 100,
                    "completion_tokens": 25,
                }
            }
        ),
    )
    ds_usage = ds_adapter.extract_usage(ds_rec)
    assert ds_usage.state is UsageState.COMPLETE
    assert ds_usage.buckets is not None
    assert ds_usage.buckets.cache_read == 400
    assert ds_usage.buckets.uncached_input == 100

    chat_adapter = get_dialect_adapter("openai_chat")
    chat_missing_details = CaptureRecord(
        record_id="chat-1",
        timestamp=1.0,
        identity=EndpointIdentity("openai_chat", "openai", "gpt-4o", "/v1/chat/completions"),
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        ),
        response_body=freeze_json({"usage": {"prompt_tokens": 100, "completion_tokens": 10}}),
    )
    assert chat_adapter.extract_usage(chat_missing_details).state is UsageState.INCOMPLETE

    gem_adapter = get_dialect_adapter("gemini_generate_content")
    gem_rec = CaptureRecord(
        record_id="gem-1",
        timestamp=1.0,
        identity=EndpointIdentity(
            "gemini_generate_content", "gemini", "gemini-2.5-pro", "/v1/models:generateContent"
        ),
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}),
        response_body=freeze_json(
            {
                "usageMetadata": {
                    "promptTokenCount": 300,
                    "cachedContentTokenCount": 200,
                    "candidatesTokenCount": 30,
                }
            }
        ),
    )
    gem_usage = gem_adapter.extract_usage(gem_rec)
    assert gem_usage.state is UsageState.COMPLETE
    assert gem_usage.buckets is not None
    assert gem_usage.buckets.cache_read == 200
    assert gem_usage.buckets.uncached_input == 100


# --------------------------------------------------------------------------------------
# v0.2.1: streamed usage for the usage-only dialects (specs/06, gates S1-S4)
# --------------------------------------------------------------------------------------

CHAT_WITHOUT_DETAILS: dict[str, Any] = {"prompt_tokens": 100, "completion_tokens": 7}
CHAT_CACHED_ABOVE_PROMPT: dict[str, Any] = {
    "prompt_tokens": 10,
    "completion_tokens": 7,
    "prompt_tokens_details": {"cached_tokens": 64},
}
DEEPSEEK_WITHOUT_CACHE_COUNTS: dict[str, Any] = {"prompt_tokens": 20, "completion_tokens": 9}
DEEPSEEK_TOTALS_DISAGREE: dict[str, Any] = {**DEEPSEEK_FINAL_USAGE, "prompt_tokens": 21}
GEMINI_WITHOUT_CACHED_COUNT: dict[str, Any] = {
    "promptTokenCount": 300,
    "candidatesTokenCount": 42,
}
GEMINI_CACHED_ABOVE_PROMPT: dict[str, Any] = {
    "promptTokenCount": 100,
    "cachedContentTokenCount": 200,
    "candidatesTokenCount": 42,
}

# How a stream ends: with its sentinel, before the sentinel, or with an error frame.
Ending = Literal["done", "cut", "error"]


def _chat_stream(
    usage: dict[str, Any] | None,
    ending: Ending = "done",
    *,
    with_usage_key: bool = True,
) -> bytes:
    """A Chat Completions stream with model text in its content chunks."""
    chunks = [
        chat_chunk("hel", with_usage_key=with_usage_key),
        chat_chunk(MODEL_TEXT, with_usage_key=with_usage_key),
    ]
    tail = [chat_final(usage)] if usage is not None else []
    if ending == "cut":
        return data_frames(*chunks, *tail)
    if ending == "error":
        return data_frames(*chunks, *tail, CHAT_ERROR, CHAT_DONE)
    return data_frames(*chunks, *tail, CHAT_DONE)


def _deepseek_stream(*usages: dict[str, Any] | None, ending: Ending = "done") -> bytes:
    """A DeepSeek stream with one content chunk per usage object; None leaves usage out."""
    texts = ["hel", MODEL_TEXT]
    assert 0 < len(usages) <= len(texts)
    chunks = [deepseek_chunk(text, usage) for text, usage in zip(texts, usages, strict=False)]
    if ending == "cut":
        return data_frames(*chunks)
    if ending == "error":
        return data_frames(*chunks, CHAT_ERROR, CHAT_DONE)
    return data_frames(*chunks, CHAT_DONE)


def _gemini_stream(meta: dict[str, Any] | None, ending: Ending = "done") -> bytes:
    """A Gemini stream; it has no sentinel, so it ends when the connection ends."""
    frames = [gemini_frame("hel", meta), gemini_frame(MODEL_TEXT, meta)]
    if ending == "cut":  # the connection closed before the second frame
        return data_frames(frames[0])
    if ending == "error":
        return data_frames(*frames, GEMINI_ERROR)
    return data_frames(*frames)


def _usage(identity: EndpointIdentity, stream: bytes) -> UsageObservation:
    """What the dialect's adapter makes of ``stream`` once the proxy has captured it."""
    return get_dialect_adapter(identity.dialect).extract_usage(capture(identity, stream))


def test_s1_chat_completions_stream_reports_the_usage_of_the_last_frame() -> None:
    usage = _usage(CHAT, _chat_stream(CHAT_FINAL_USAGE))

    assert usage.state is UsageState.COMPLETE
    assert usage.buckets == TokenBuckets(
        uncached_input=36,
        cache_read=64,
        cache_writes=(),
        output=7,
        total_input=100,
        reported_hit_ratio=0.64,
    )
    assert dict(usage.raw_fields) == CHAT_FINAL_USAGE


def test_s1_chat_completions_stream_without_a_usage_frame_is_missing_and_carries_no_text(
) -> None:
    # The client never set stream_options.include_usage, so no chunk mentions usage at all.
    usage = _usage(CHAT, _chat_stream(None, with_usage_key=False))

    assert usage.state is UsageState.MISSING
    assert usage.buckets is None
    assert usage.notes == ("missing_usage_in_stream",)
    assert dict(usage.raw_fields) == {}


@pytest.mark.parametrize(
    ("final", "state", "note"),
    [
        pytest.param(
            CHAT_WITHOUT_DETAILS,
            UsageState.INCOMPLETE,
            "missing_prompt_tokens_details_cached_tokens",
            id="without-cached-tokens",
        ),
        pytest.param(
            CHAT_CACHED_ABOVE_PROMPT,
            UsageState.INVALID,
            "cached_tokens_exceeds_prompt_tokens",
            id="cached-above-prompt",
        ),
    ],
)
def test_s1_chat_completions_stream_keeps_the_non_streamed_verdicts(
    final: dict[str, Any], state: UsageState, note: str
) -> None:
    usage = _usage(CHAT, _chat_stream(final))

    assert usage.state is state
    assert usage.buckets is None
    assert usage.notes == (note,)
    assert dict(usage.raw_fields) == final  # the usage object, never the model output


@pytest.mark.parametrize("ending", ["cut", "error"])
def test_s1_chat_completions_stream_that_did_not_end_cleanly_is_missing(ending: Ending) -> None:
    stream = _chat_stream(CHAT_FINAL_USAGE, ending)

    assert capture(CHAT, stream).response_complete is False  # the proxy's own verdict
    usage = _usage(CHAT, stream)
    assert usage.state is UsageState.MISSING
    assert usage.buckets is None
    assert usage.notes == ("response_incomplete_or_interrupted",)


def test_s2_deepseek_stream_reports_the_last_cumulative_usage_not_their_sum() -> None:
    earlier = {**DEEPSEEK_FINAL_USAGE, "completion_tokens": 3, "total_tokens": 23}
    usage = _usage(DEEPSEEK, _deepseek_stream(earlier, DEEPSEEK_FINAL_USAGE))

    assert usage.state is UsageState.COMPLETE
    assert usage.buckets == TokenBuckets(
        uncached_input=4,
        cache_read=16,
        cache_writes=(),
        output=9,  # never 3 + 9
        total_input=20,
        reported_hit_ratio=0.8,
    )
    assert dict(usage.raw_fields) == DEEPSEEK_FINAL_USAGE


def test_s2_deepseek_stream_whose_prompt_total_disagrees_with_its_parts_is_invalid() -> None:
    usage = _usage(DEEPSEEK, _deepseek_stream(DEEPSEEK_TOTALS_DISAGREE))  # 16 + 4 is not 21

    assert usage.state is UsageState.INVALID
    assert usage.buckets is None
    assert usage.notes == ("prompt_tokens_mismatch_hit_plus_miss",)
    assert dict(usage.raw_fields) == DEEPSEEK_TOTALS_DISAGREE


def test_s2_deepseek_stream_without_cache_counts_is_incomplete() -> None:
    usage = _usage(DEEPSEEK, _deepseek_stream(DEEPSEEK_WITHOUT_CACHE_COUNTS))

    assert usage.state is UsageState.INCOMPLETE
    assert usage.buckets is None
    assert usage.notes == ("missing_deepseek_cache_hit_or_miss_tokens",)
    assert dict(usage.raw_fields) == DEEPSEEK_WITHOUT_CACHE_COUNTS


def test_s2_deepseek_stream_without_usage_is_missing() -> None:
    usage = _usage(DEEPSEEK, _deepseek_stream(None, None))

    assert usage.state is UsageState.MISSING
    assert usage.buckets is None
    assert usage.notes == ("missing_usage_in_stream",)
    assert dict(usage.raw_fields) == {}


@pytest.mark.parametrize("ending", ["cut", "error"])
def test_s2_deepseek_stream_that_did_not_end_cleanly_is_missing(ending: Ending) -> None:
    stream = _deepseek_stream(DEEPSEEK_FINAL_USAGE, DEEPSEEK_FINAL_USAGE, ending=ending)

    assert capture(DEEPSEEK, stream).response_complete is False  # the proxy's own verdict
    usage = _usage(DEEPSEEK, stream)
    assert usage.state is UsageState.MISSING
    assert usage.buckets is None
    assert usage.notes == ("response_incomplete_or_interrupted",)


def _gemini_frames() -> list[dict[str, Any]]:
    """Three chunks whose usage snapshot grows: 10, then 25, then 42 output tokens."""
    return [
        gemini_frame("hel", gemini_meta(10)),
        gemini_frame(MODEL_TEXT, gemini_meta(25)),
        gemini_frame("lo", gemini_meta(42)),
    ]


def test_s3_gemini_stream_keeps_only_the_last_cumulative_snapshot() -> None:
    usage = _usage(GEMINI, data_frames(*_gemini_frames()))

    assert usage.state is UsageState.COMPLETE
    assert usage.buckets == TokenBuckets(
        uncached_input=100,
        cache_read=200,
        cache_writes=(),
        output=42,  # never 10 + 25 + 42
        total_input=300,
        reported_hit_ratio=200 / 300,
    )
    assert dict(usage.raw_fields) == gemini_meta(42)


@pytest.mark.parametrize(
    ("meta", "state", "note"),
    [
        pytest.param(
            GEMINI_WITHOUT_CACHED_COUNT,
            UsageState.INCOMPLETE,
            "incomplete_gemini_usageMetadata",
            id="without-cached-count",
        ),
        pytest.param(
            GEMINI_CACHED_ABOVE_PROMPT,
            UsageState.INVALID,
            "cachedContentTokenCount_exceeds_promptTokenCount",
            id="cached-above-prompt",
        ),
    ],
)
def test_s3_gemini_stream_keeps_the_non_streamed_verdicts(
    meta: dict[str, Any], state: UsageState, note: str
) -> None:
    usage = _usage(GEMINI, _gemini_stream(meta))

    assert usage.state is state
    assert usage.buckets is None
    assert usage.notes == (note,)
    assert dict(usage.raw_fields) == meta


def test_s3_gemini_stream_without_usage_is_missing() -> None:
    usage = _usage(GEMINI, _gemini_stream(None))

    assert usage.state is UsageState.MISSING
    assert usage.buckets is None
    assert usage.notes == ("missing_usage_in_stream",)
    assert dict(usage.raw_fields) == {}


def test_s3_gemini_stream_that_ends_in_an_error_frame_is_missing() -> None:
    stream = _gemini_stream(gemini_meta(42), ending="error")

    assert capture(GEMINI, stream).response_complete is False  # the proxy's own verdict
    usage = _usage(GEMINI, stream)
    assert usage.state is UsageState.MISSING
    assert usage.buckets is None
    assert usage.notes == ("response_incomplete_or_interrupted",)


def test_s3_gemini_stream_that_merely_stops_passes_as_finished() -> None:
    # A known limit, pinned so that changing it is a deliberate act: a Gemini stream has no
    # terminal frame, so a connection that closes early looks exactly like one that ended.
    stream = _gemini_stream(gemini_meta(42), ending="cut")

    record = capture(GEMINI, stream)
    assert record.response_complete is True
    # the record deep-freezes its response, so compare a thawed copy
    assert thaw_json(record.response_body)["events"] == [gemini_frame("hel", gemini_meta(42))]


def test_s3_gemini_usage_metadata_accepts_the_snake_case_alias() -> None:
    frame = gemini_frame("hel", None)
    frame["usage_metadata"] = gemini_meta(42)
    usage = _usage(GEMINI, data_frames(frame))

    assert usage.state is UsageState.COMPLETE
    assert usage.buckets is not None and usage.buckets.output == 42


def test_s3_gemini_usage_metadata_skips_a_value_that_is_not_an_object() -> None:
    # Deliberately not v0.1 behaviour: v0.1 took the first truthy value and gave up when it
    # was not an object. The alias behind it is still evidence, so it is used.
    frame = gemini_frame("hel", None)
    frame["usageMetadata"] = 5
    frame["usage_metadata"] = gemini_meta(42)
    usage = _usage(GEMINI, data_frames(frame))

    assert usage.state is UsageState.COMPLETE
    assert usage.buckets is not None and usage.buckets.output == 42


# One stream per dialect and usage state, interrupted streams included. Every one of them
# carries model output that no default report may contain.
_S4_CASES = [
    pytest.param(
        CHAT,
        _chat_stream(None, with_usage_key=False),
        UsageState.MISSING,
        id="chat-missing",
    ),
    pytest.param(
        CHAT, _chat_stream(CHAT_WITHOUT_DETAILS), UsageState.INCOMPLETE, id="chat-incomplete"
    ),
    pytest.param(
        CHAT, _chat_stream(CHAT_CACHED_ABOVE_PROMPT), UsageState.INVALID, id="chat-invalid"
    ),
    pytest.param(
        CHAT, _chat_stream(CHAT_FINAL_USAGE, "cut"), UsageState.MISSING, id="chat-interrupted"
    ),
    pytest.param(DEEPSEEK, _deepseek_stream(None, None), UsageState.MISSING, id="deepseek-missing"),
    pytest.param(
        DEEPSEEK,
        _deepseek_stream(DEEPSEEK_WITHOUT_CACHE_COUNTS),
        UsageState.INCOMPLETE,
        id="deepseek-incomplete",
    ),
    pytest.param(
        DEEPSEEK,
        _deepseek_stream(DEEPSEEK_TOTALS_DISAGREE),
        UsageState.INVALID,
        id="deepseek-invalid",
    ),
    pytest.param(
        DEEPSEEK,
        _deepseek_stream(DEEPSEEK_FINAL_USAGE, DEEPSEEK_FINAL_USAGE, ending="error"),
        UsageState.MISSING,
        id="deepseek-error-frame",
    ),
    pytest.param(GEMINI, _gemini_stream(None), UsageState.MISSING, id="gemini-missing"),
    pytest.param(
        GEMINI,
        _gemini_stream(GEMINI_WITHOUT_CACHED_COUNT),
        UsageState.INCOMPLETE,
        id="gemini-incomplete",
    ),
    pytest.param(
        GEMINI, _gemini_stream(GEMINI_CACHED_ABOVE_PROMPT), UsageState.INVALID, id="gemini-invalid"
    ),
    pytest.param(
        GEMINI,
        _gemini_stream(gemini_meta(42), ending="error"),
        UsageState.MISSING,
        id="gemini-error-frame",
    ),
]

@pytest.mark.parametrize(("identity", "stream", "state"), _S4_CASES)
def test_s4_streamed_usage_never_carries_model_text_into_the_default_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    identity: EndpointIdentity,
    stream: bytes,
    state: UsageState,
) -> None:
    record = capture(identity, stream)
    capture_path = tmp_path / "capture.jsonl"
    capture_path.write_text(dump_capture_record(record) + "\n", encoding="utf-8")
    adapter = get_dialect_adapter(identity.dialect)
    assert adapter.extract_usage(record).state is state  # the case really is in that state

    capsys.readouterr()
    assert main(["analyze", str(capture_path), "--format", "json"]) == 0

    report = capsys.readouterr().out
    assert MODEL_TEXT not in report
    for leaked in ('"choices"', '"delta"', '"candidates"', '"parts"'):
        assert leaked not in report


def test_openai_responses_zero_cache_write_tokens_omitted_and_negative_invalid() -> None:
    adapter = get_dialect_adapter("openai_responses")
    identity = EndpointIdentity("openai_responses", "openai", "gpt-6.1-sol", "/v1/responses")

    def rec(details: dict[str, Any], input_tokens: int) -> CaptureRecord:
        return CaptureRecord(
            record_id="oai-cw",
            timestamp=1.0,
            identity=identity,
            request_headers=freeze_json({}),
            captured_body_bytes=None,
            parsed_body=freeze_json({"model": "gpt-6.1-sol", "input": []}),
            response_body=freeze_json(
                {
                    "id": "resp_cw",
                    "usage": {
                        "input_tokens": input_tokens,
                        "input_tokens_details": details,
                        "output_tokens": 5,
                    },
                }
            ),
            response_complete=True,
        )

    zero_write = adapter.extract_usage(
        rec(
            {
                "cached_tokens": 4096,
                "cache_write_tokens": 0,
                "cache_write_tokens_details": {"ttl_30m_tokens": 0, "ttl_12h_tokens": 0},
            },
            4259,
        )
    )
    assert zero_write.state is UsageState.COMPLETE
    assert zero_write.buckets is not None
    assert zero_write.buckets.cache_writes == ()
    assert zero_write.buckets.cache_read == 4096
    assert zero_write.buckets.uncached_input == 163

    negative_write = adapter.extract_usage(
        rec({"cached_tokens": 0, "cache_write_tokens": -1}, 1000)
    )
    assert negative_write.state is UsageState.INVALID

    positive_write = adapter.extract_usage(
        rec({"cached_tokens": 0, "cache_write_tokens": 500}, 1000)
    )
    assert positive_write.state is UsageState.COMPLETE
    assert positive_write.buckets is not None
    assert positive_write.buckets.cache_writes == (CacheWriteBucket("default", 500),)
    assert positive_write.buckets.uncached_input == 500


def test_captured_third_party_relay_gpt61_hit_fixture() -> None:
    fixture = FIXTURES_DIR / "captured" / "openai_responses_relay_gpt61_hit.jsonl"
    raw_text = fixture.read_text(encoding="utf-8")
    assert "surplus" not in raw_text.lower()
    assert "sk-" not in raw_text
    assert '"safety_identifier":"user-' not in raw_text

    records = load_capture_records(fixture)
    assert len(records) == 2
    t1, t2 = DiagnosisSession().diagnose_all(records)

    assert t1.lineage.status is LineageStatus.NO_BASELINE
    assert t1.usage.state is UsageState.COMPLETE
    assert t1.usage.buckets is not None
    assert t1.usage.buckets.cache_writes == ()
    assert t1.usage.buckets.total_input == 4240
    assert t1.cost_estimate is not None
    assert t1.cost_estimate.estimated_input_cost_usd == pytest.approx(4240 * 2.00 / 1_000_000)

    assert t2.lineage.status is LineageStatus.MATCHED
    assert t2.lineage.match_basis == "history_continuation"
    assert t2.lineage.parent_record_id == records[0].record_id
    assert t2.observed_changes
    assert all(c.change_kind is ChangeKind.NORMAL_APPEND for c in t2.observed_changes)
    assert all(
        c.span is not None and c.span.space is OffsetSpace.WIRE_RAW for c in t2.observed_changes
    )
    assert t2.usage.state is UsageState.COMPLETE
    assert t2.usage.buckets is not None
    assert t2.usage.buckets.cache_read == 4096
    assert t2.usage.buckets.uncached_input == 163
    assert t2.usage.buckets.cache_writes == ()
    assert t2.cost_estimate is not None
    assert t2.cost_estimate.estimated_input_cost_usd == pytest.approx(
        (163 * 2.00 + 4096 * 0.10) / 1_000_000
    )

    assert main(["check", str(fixture)]) == 0
