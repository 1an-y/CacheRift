"""Deterministic micro-probe scenarios emitted as raw UTF-8 bytes."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SCENARIO_ORDER: tuple[str, ...] = (
    "baseline_identical_pair",
    "wire_vs_key_reorder",
    "tool_tail_insertion",
    "gpt62_instructions_placement",
    "anthropic_breakpoints",
)

_BOTH_DIALECTS = ("openai_responses", "anthropic_messages")
SCENARIO_DIALECTS: dict[str, tuple[str, ...]] = {
    "baseline_identical_pair": _BOTH_DIALECTS,
    "wire_vs_key_reorder": _BOTH_DIALECTS,
    "tool_tail_insertion": _BOTH_DIALECTS,
    "gpt62_instructions_placement": ("openai_responses",),
    "anthropic_breakpoints": ("anthropic_messages",),
}

# Output is capped per request so the output half of the cost bound is exact.
MAX_OUTPUT_TOKENS = 64
_DEFAULT_PREFIX_TOKENS = 2200
# Margin above capability min_cache_tokens; not a fixed vendor threshold.
_THRESHOLD_MARGIN = 1.1
# ASCII English probe prose averages ~4.5-4.8 UTF-8 bytes per BPE token, so 5 bytes per target
# token avoids undershooting real token counts while heuristic_bytes_div_4 stays conservative.
_BYTES_PER_TARGET_TOKEN = 5

_Q1 = "Reply with the single word: alpha."
_Q2 = "Reply with the single word: bravo."
_ASSISTANT_REPLY = "alpha"
_TOOL_DESCRIPTION = "Look up a record by its key."


@dataclass(frozen=True, slots=True)
class ProbeStep:
    """One planned request; ``parent`` names the step it is compared against."""

    label: str
    intent: str
    body: bytes
    parent: str | None


@dataclass(frozen=True, slots=True)
class ProbeScenario:
    name: str
    steps: tuple[ProbeStep, ...]


def _prefix_text(nonce: str, min_bytes: int) -> str:
    header = f"CacheRift probe corpus {nonce}. 以下为确定性占位文本，仅用于前缀缓存探针。"
    lines = [header]
    byte_count = len(header.encode("utf-8"))
    rule = 0
    while byte_count < min_bytes:
        line = (
            f"Rule {rule:04d}: keep tool schemas, system text and conversation history "
            "byte-stable across turns so the prompt cache prefix stays valid."
        )
        lines.append(line)
        byte_count += 1 + len(line.encode("utf-8"))
        rule += 1
    return "\n".join(lines)


def _schema(*, reorder: bool = False) -> dict[str, Any]:
    properties = {"key": {"type": "string"}}
    required = ["key"]
    if reorder:
        return {"required": required, "properties": properties, "type": "object"}
    return {"type": "object", "properties": properties, "required": required}


def _tool(dialect: str, name: str, *, reorder: bool = False) -> dict[str, Any]:
    """One function tool; ``reorder`` changes only the key order inside its parameter schema."""
    schema = _schema(reorder=reorder)
    if dialect == "anthropic_messages":
        tool: dict[str, Any] = {
            "name": name,
            "description": _TOOL_DESCRIPTION,
            "input_schema": schema,
        }
    else:
        tool = {
            "type": "function",
            "name": name,
            "description": _TOOL_DESCRIPTION,
            "parameters": schema,
        }
    return tool  # only the schema's key order is perturbed; the tool's own keys stay put


def _message(dialect: str, role: str, text: str) -> dict[str, Any]:
    if dialect == "anthropic_messages":
        return {"role": role, "content": [{"type": "text", "text": text}]}
    return {"role": role, "content": text}


def _cache_control(ttl: str) -> dict[str, str]:
    return {"type": "ephemeral", "ttl": ttl}


def _envelope(
    dialect: str,
    model: str,
    *,
    system: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    ttl: str,
    cache_key: str | None = None,
) -> dict[str, Any]:
    if dialect == "anthropic_messages":
        return {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "tools": tools,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": _cache_control(ttl),
                }
            ],
            "messages": messages,
        }
    body: dict[str, Any] = {
        "model": model,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "tools": tools,
    }
    if system is not None:
        body["instructions"] = system
    if cache_key is not None:
        body["prompt_cache_key"] = cache_key
    body["input"] = messages
    return body


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _wire_variant(obj: dict[str, Any]) -> bytes:
    """Same parsed structure as ``_canonical``; only whitespace and \\uXXXX escapes differ."""
    text = json.dumps(obj, indent=2, ensure_ascii=True)
    return text.replace("CacheRift", "C\\u0061cheRift").encode("utf-8")


def _history(dialect: str) -> list[dict[str, Any]]:
    return [
        _message(dialect, "user", _Q1),
        _message(dialect, "assistant", _ASSISTANT_REPLY),
        _message(dialect, "user", _Q2),
    ]


def _baseline(
    dialect: str, model: str, prefix: str, ttl: str, *, cache_key: str | None = None
) -> ProbeScenario:
    lookup = _tool(dialect, "lookup_record")
    shared: dict[str, Any] = {
        "system": prefix,
        "tools": [lookup],
        "ttl": ttl,
        "cache_key": cache_key,
    }
    round1 = _envelope(dialect, model, messages=[_message(dialect, "user", _Q1)], **shared)
    round2 = _envelope(dialect, model, messages=[_message(dialect, "user", _Q2)], **shared)
    return ProbeScenario(
        "baseline_identical_pair",
        (
            ProbeStep("round1", "cold write of the shared prefix", _canonical(round1), None),
            ProbeStep(
                "round2",
                "same prefix; only the final question differs",
                _canonical(round2),
                "round1",
            ),
        ),
    )


def _wire_vs_key_reorder(dialect: str, model: str, prefix: str, ttl: str) -> ProbeScenario:
    shared: dict[str, Any] = {"system": prefix, "ttl": ttl}
    messages = [_message(dialect, "user", _Q1)]
    base = _envelope(
        dialect, model, messages=messages, tools=[_tool(dialect, "lookup_record")], **shared
    )
    reordered = _envelope(
        dialect,
        model,
        messages=messages,
        tools=[_tool(dialect, "lookup_record", reorder=True)],
        **shared,
    )
    return ProbeScenario(
        "wire_vs_key_reorder",
        (
            ProbeStep("base", "canonical compact serialization", _canonical(base), None),
            ProbeStep(
                "wire_only",
                "same structure; whitespace and unicode escapes only",
                _wire_variant(base),
                "base",
            ),
            ProbeStep(
                "reordered_keys",
                "same as base except the key order inside tools[0]",
                _canonical(reordered),
                "base",
            ),
        ),
    )


def _tool_tail_insertion(dialect: str, model: str, prefix: str, ttl: str) -> ProbeScenario:
    lookup = _tool(dialect, "lookup_record")
    search = _tool(dialect, "search_records")
    shared: dict[str, Any] = {"system": prefix, "ttl": ttl}
    round1 = _envelope(
        dialect, model, messages=[_message(dialect, "user", _Q1)], tools=[lookup], **shared
    )
    round2 = _envelope(
        dialect, model, messages=_history(dialect), tools=[lookup, search], **shared
    )
    return ProbeScenario(
        "tool_tail_insertion",
        (
            ProbeStep("round1", "one tool", _canonical(round1), None),
            ProbeStep(
                "round2",
                "a second tool appended to tools; history grows by one turn",
                _canonical(round2),
                "round1",
            ),
        ),
    )


def _gpt62_instructions_placement(dialect: str, model: str, prefix: str, ttl: str) -> ProbeScenario:
    tools = [_tool(dialect, "lookup_record")]
    user_turn = [_message(dialect, "user", _Q1)]
    top_round1 = _envelope(
        dialect, model, system=prefix, messages=user_turn, tools=tools, ttl=ttl
    )
    top_round2 = _envelope(
        dialect, model, system=prefix, messages=_history(dialect), tools=tools, ttl=ttl
    )
    developer = _message(dialect, "developer", prefix)
    dev_round1 = _envelope(
        dialect, model, system=None, messages=[developer, *user_turn], tools=tools, ttl=ttl
    )
    dev_round2 = _envelope(
        dialect, model, system=None, messages=[developer, *_history(dialect)], tools=tools, ttl=ttl
    )
    return ProbeScenario(
        "gpt62_instructions_placement",
        (
            ProbeStep(
                "top_round1",
                "prefix in top-level instructions",
                _canonical(top_round1),
                None,
            ),
            ProbeStep(
                "top_round2",
                "top-level instructions kept; history grows",
                _canonical(top_round2),
                "top_round1",
            ),
            ProbeStep(
                "dev_round1",
                "same prefix as a developer message",
                _canonical(dev_round1),
                None,
            ),
            ProbeStep(
                "dev_round2",
                "developer message kept; history grows",
                _canonical(dev_round2),
                "dev_round1",
            ),
        ),
    )


def _anthropic_breakpoints(dialect: str, model: str, prefix: str, ttl: str) -> ProbeScenario:
    dialect = "anthropic_messages"
    shared: dict[str, Any] = {
        "system": prefix,
        "tools": [_tool(dialect, "lookup_record")],
        "ttl": ttl,
    }
    write = _envelope(
        dialect, model, messages=[_message(dialect, "user", _Q1)], **shared
    )
    read = _envelope(dialect, model, messages=_history(dialect), **shared)
    slide = _envelope(dialect, model, messages=_history(dialect), **shared)
    slide["messages"][-1]["content"][0]["cache_control"] = _cache_control(ttl)
    return ProbeScenario(
        "anthropic_breakpoints",
        (
            ProbeStep("write", "system breakpoint; cold write", _canonical(write), None),
            ProbeStep(
                "read",
                "breakpoint unchanged; history grows",
                _canonical(read),
                "write",
            ),
            ProbeStep(
                "slide",
                "a new breakpoint on the last message",
                _canonical(slide),
                "read",
            ),
        ),
    )


def build_scenarios(
    dialect: str,
    model: str,
    *,
    ttl: str = "5m",
    nonce: str = "probe",
    min_cache_tokens: int | None = None,
    prefix_tokens: int | None = None,
) -> tuple[ProbeScenario, ...]:
    """Build every scenario that applies to ``dialect``, in ``SCENARIO_ORDER``.

    The result is fully deterministic for a given ``nonce``; a different nonce per run keeps one
    run from reading the cache written by another.
    """
    if prefix_tokens is not None:
        if prefix_tokens < 1:
            raise ValueError("prefix_tokens must be at least 1")
        target_tokens = prefix_tokens
    else:
        target_tokens = max(
            _DEFAULT_PREFIX_TOKENS, math.ceil((min_cache_tokens or 0) * _THRESHOLD_MARGIN)
        )

    def prefix(name: str) -> str:
        return _prefix_text(f"{nonce}:{name}", target_tokens * _BYTES_PER_TARGET_TOKEN)

    builders: dict[str, Callable[[str, str, str, str], ProbeScenario]] = {
        "baseline_identical_pair": lambda d, m, p, t: _baseline(
            d, m, p, t, cache_key=f"{nonce}:baseline_identical_pair"
        ),
        "wire_vs_key_reorder": _wire_vs_key_reorder,
        "tool_tail_insertion": _tool_tail_insertion,
        "gpt62_instructions_placement": _gpt62_instructions_placement,
        "anthropic_breakpoints": _anthropic_breakpoints,
    }
    return tuple(
        builders[name](dialect, model, prefix(name), ttl)
        for name in SCENARIO_ORDER
        if dialect in SCENARIO_DIALECTS[name]
    )
