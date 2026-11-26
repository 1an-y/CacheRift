from __future__ import annotations

from pathlib import Path

from cacherift.capabilities.registry import CapabilityRegistry
from cacherift.core.freeze import freeze_json
from cacherift.core.models import (
    CaptureRecord,
    DisplayRole,
    EndpointIdentity,
    OffsetSpace,
    UsageState,
)
from cacherift.dialects.base import get_dialect_adapter
from cacherift.io.capture import load_capture_records

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
