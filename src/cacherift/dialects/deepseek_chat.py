"""DeepSeek Chat dialect adapter (partial usage_only support)."""

from __future__ import annotations

from collections.abc import Mapping

from cacherift.core.models import CaptureRecord, ProviderDiagnostics, UsageObservation
from cacherift.dialects.base import (
    finalize_usage_buckets,
    incomplete_usage,
    invalid_usage,
    missing_usage,
)
from cacherift.dialects.openai_chat import OpenAIChatAdapter


class DeepSeekChatAdapter(OpenAIChatAdapter):
    dialect_name = "deepseek_chat"

    def extract_usage(self, record: CaptureRecord) -> UsageObservation:
        if not record.response_complete:
            return missing_usage(record, "response_incomplete_or_interrupted")
        body = record.response_body
        if not isinstance(body, Mapping) or not isinstance(body.get("usage"), Mapping):
            return missing_usage(record, "missing_usage")

        usage = body["usage"]
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        completion = usage.get("completion_tokens")
        if hit is None or miss is None or completion is None:
            return incomplete_usage(usage, "missing_deepseek_cache_hit_or_miss_tokens")

        hit_int = int(hit)
        miss_int = int(miss)
        prompt_tokens = usage.get("prompt_tokens")
        if prompt_tokens is not None and int(prompt_tokens) != hit_int + miss_int:
            return invalid_usage(usage, "prompt_tokens_mismatch_hit_plus_miss")

        return finalize_usage_buckets(
            uncached_input=miss_int,
            cache_read=hit_int,
            cache_writes=(),
            output=int(completion),
            raw_fields=usage,
        )

    def extract_provider_diagnostics(self, record: CaptureRecord) -> ProviderDiagnostics | None:
        del record
        return None
