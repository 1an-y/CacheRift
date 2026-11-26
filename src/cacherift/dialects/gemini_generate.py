"""Gemini generateContent dialect adapter (partial usage_only support)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cacherift.core.freeze import canonical_project_bytes, freeze_json, sha256_hex
from cacherift.core.models import (
    CacheConfigFingerprint,
    CapabilityInfo,
    CaptureRecord,
    DisplayRole,
    OrderedUnit,
    ProviderDiagnostics,
    UsageObservation,
)
from cacherift.dialects.base import (
    compute_wire_spans,
    finalize_usage_buckets,
    incomplete_usage,
    invalid_usage,
    make_ordered_unit,
    missing_usage,
)


class GeminiGenerateAdapter:
    dialect_name = "gemini_generate_content"

    def extract_ordered_units(
        self,
        record: CaptureRecord,
        capability: CapabilityInfo,
    ) -> tuple[tuple[OrderedUnit, ...], CacheConfigFingerprint]:
        del capability
        body = record.parsed_body if isinstance(record.parsed_body, Mapping) else {}
        wire_spans = compute_wire_spans(record)
        units: list[OrderedUnit] = []

        tools = body.get("tools")
        if isinstance(tools, Sequence) and not isinstance(tools, (str, bytes)):
            for idx, tool in enumerate(tools):
                units.append(
                    make_ordered_unit(
                        unit_id=f"tool[{idx}]",
                        json_path=f"$.tools[{idx}]",
                        display_role=DisplayRole.TOOLS,
                        projected_value=tool,
                        wire_spans=wire_spans,
                        origin_map=record.origin_map,
                    )
                )

        if "systemInstruction" in body or "system_instruction" in body:
            sys_key = "systemInstruction" if "systemInstruction" in body else "system_instruction"
            if body.get(sys_key) is not None:
                units.append(
                    make_ordered_unit(
                        unit_id="system_instruction",
                        json_path=f"$.{sys_key}",
                        display_role=DisplayRole.SYSTEM_OR_INSTRUCTIONS,
                        projected_value=body[sys_key],
                        wire_spans=wire_spans,
                        origin_map=record.origin_map,
                    )
                )

        contents = body.get("contents")
        if isinstance(contents, Sequence) and not isinstance(contents, (str, bytes)):
            last_idx = len(contents) - 1
            for idx, item in enumerate(contents):
                role = item.get("role", "content") if isinstance(item, Mapping) else "content"
                display_role = (
                    DisplayRole.CURRENT_INPUT if idx == last_idx else DisplayRole.HISTORY_MESSAGE
                )
                units.append(
                    make_ordered_unit(
                        unit_id=f"contents[{idx}]:{role}",
                        json_path=f"$.contents[{idx}]",
                        display_role=display_role,
                        projected_value=item,
                        wire_spans=wire_spans,
                        origin_map=record.origin_map,
                    )
                )

        gen_cfg = body.get("generationConfig")
        schema = gen_cfg.get("responseSchema") if isinstance(gen_cfg, Mapping) else None
        thinking = gen_cfg.get("thinkingConfig") if isinstance(gen_cfg, Mapping) else None
        cache_params: list[tuple[str, str, Any]] = []
        for key in ("cachedContent", "cached_content"):
            if key in body and body[key] is not None:
                cache_params.append((f"$.{key}", key, freeze_json(body[key])))

        cfg = CacheConfigFingerprint(
            model=str(body.get("model", record.identity.model)),
            reasoning_or_thinking=freeze_json(thinking) if thinking is not None else None,
            output_schema_sha256=sha256_hex(canonical_project_bytes(schema))
            if schema is not None
            else None,
            service_tier=None,
            cache_params=tuple(cache_params),
        )
        return tuple(units), cfg

    def extract_usage(self, record: CaptureRecord) -> UsageObservation:
        if not record.response_complete:
            return missing_usage(record, "response_incomplete_or_interrupted")
        body = record.response_body
        if not isinstance(body, Mapping):
            return missing_usage(record, "missing_response_body")
        meta = body.get("usageMetadata") or body.get("usage_metadata")
        if not isinstance(meta, Mapping):
            return missing_usage(record, "missing_usageMetadata")

        prompt = meta.get("promptTokenCount")
        cached = meta.get("cachedContentTokenCount")
        candidates = meta.get("candidatesTokenCount")
        if prompt is None or cached is None or candidates is None:
            return incomplete_usage(meta, "incomplete_gemini_usageMetadata")

        prompt_int = int(prompt)
        cached_int = int(cached)
        if cached_int > prompt_int:
            return invalid_usage(meta, "cachedContentTokenCount_exceeds_promptTokenCount")

        return finalize_usage_buckets(
            uncached_input=prompt_int - cached_int,
            cache_read=cached_int,
            cache_writes=(),
            output=int(candidates),
            raw_fields=meta,
        )

    def extract_provider_diagnostics(self, record: CaptureRecord) -> ProviderDiagnostics | None:
        del record
        return None

    def extract_previous_response_ref(self, record: CaptureRecord) -> str | None:
        del record
        return None
