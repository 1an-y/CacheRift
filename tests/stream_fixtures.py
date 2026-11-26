"""Synthetic SSE streams for the usage-only dialects, shared by the adapter and proxy tests.

Every frame here is a hand-written contract sample, not a vendor capture, so none of it belongs
under tests/fixtures/captured/. The module is imported by the test files next to it.
"""

from __future__ import annotations

import json
from typing import Any

from cacherift.core.freeze import freeze_json
from cacherift.core.models import CaptureRecord, EndpointIdentity
from cacherift.proxy.sse import SSEAccumulator

CHAT = EndpointIdentity("openai_chat", "openai", "gpt-4o", "/v1/chat/completions")
DEEPSEEK = EndpointIdentity("deepseek_chat", "deepseek", "deepseek-chat", "/chat/completions")
GEMINI = EndpointIdentity(
    "gemini_generate_content",
    "gemini",
    "gemini-2.5-pro",
    "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
)

# Distinctive model output. No default report may ever contain it.
MODEL_TEXT = "TOP_SECRET_MODEL_OUTPUT_314159"
CHAT_DONE = "[DONE]"
CHAT_ERROR: dict[str, Any] = {
    "error": {
        "message": "The server had an error",
        "type": "server_error",
        "param": None,
        "code": None,
    }
}
GEMINI_ERROR: dict[str, Any] = {
    "error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}
}

CHAT_FINAL_USAGE: dict[str, Any] = {
    "prompt_tokens": 100,
    "completion_tokens": 7,
    "total_tokens": 107,
    "prompt_tokens_details": {"cached_tokens": 64},
}
DEEPSEEK_FINAL_USAGE: dict[str, Any] = {
    "prompt_tokens": 20,
    "completion_tokens": 9,
    "total_tokens": 29,
    "prompt_cache_hit_tokens": 16,
    "prompt_cache_miss_tokens": 4,
}


def data_frames(*payloads: Any) -> bytes:
    """SSE data frames as Chat Completions, DeepSeek and Gemini send them: no `event:` line."""
    return b"".join(
        f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n".encode()
        for payload in payloads
    )


def chat_chunk(text: str, *, with_usage_key: bool = True) -> dict[str, Any]:
    """A Chat Completions content chunk.

    With `stream_options.include_usage` on, every chunk but the last carries `"usage": null`.
    Without it, no chunk mentions usage at all.
    """
    chunk: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    if with_usage_key:
        chunk["usage"] = None
    return chunk


def chat_final(usage: dict[str, Any]) -> dict[str, Any]:
    """The last chunk of a Chat Completions stream: no choices, only the usage object."""
    return {"id": "chatcmpl-1", "choices": [], "usage": usage}


def deepseek_chunk(text: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    """A DeepSeek content chunk; the service repeats a cumulative usage object in each chunk."""
    chunk: dict[str, Any] = {
        "id": "chatcmpl-ds-1",
        "object": "chat.completion.chunk",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def gemini_meta(candidates: int) -> dict[str, Any]:
    """A cumulative usage snapshot, as a streamed GenerateContentResponse chunk carries it."""
    return {
        "promptTokenCount": 300,
        "cachedContentTokenCount": 200,
        "candidatesTokenCount": candidates,
    }


def gemini_frame(text: str, meta: dict[str, Any] | None) -> dict[str, Any]:
    """A streamed GenerateContentResponse chunk; ``meta=None`` leaves usage metadata out."""
    frame: dict[str, Any] = {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}, "index": 0}],
        "modelVersion": "gemini-2.5-pro",
    }
    if meta is not None:
        frame["usageMetadata"] = meta
    return frame


def capture(identity: EndpointIdentity, stream: bytes) -> CaptureRecord:
    """A capture record for ``stream``, marked complete exactly as the proxy would mark it.

    ``response_complete`` is the SSE accumulator's own verdict on these bytes, never a flag set
    by a test, so a stream that is cut short or ends in an error frame really is incomplete.
    """
    accumulator = SSEAccumulator(
        # A Gemini stream has no terminal frame; the proxy treats its end as the end.
        require_terminal_frame=identity.dialect != GEMINI.dialect
    )
    accumulator.feed(stream)
    accumulator.finish()
    return CaptureRecord(
        record_id="stream",
        timestamp=1.0,
        identity=identity,
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json({"model": identity.model, "messages": []}),
        response_body=freeze_json({"events": accumulator.events}),
        response_complete=accumulator.complete,
    )
