"""Wire vs. structural turn comparison distinguishing normal growth from prefix breaks."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from cacherift.core.freeze import (
    canonical_project_bytes,
    order_sensitive_equal,
    sha256_hex,
)
from cacherift.core.models import (
    ByteSpan,
    CacheConfigFingerprint,
    CaptureRecord,
    ChangeKind,
    DisplayRole,
    ObservedChange,
    OffsetSpace,
    OrderedUnit,
)
from cacherift.core.span import scan_json_spans

_CONV_ROLES = frozenset({DisplayRole.HISTORY_MESSAGE, DisplayRole.CURRENT_INPUT})
_MAX_SNIPPET_CHARS = 160


def _snippet_from_bytes(data: bytes | None, *, include: bool) -> str | None:
    if not include or data is None:
        return None
    text = data.decode("utf-8", errors="replace")
    if len(text) <= _MAX_SNIPPET_CHARS:
        return text
    return text[: _MAX_SNIPPET_CHARS - 3] + "..."


def _sorted_sha256(unit: OrderedUnit) -> str:
    obj: Any = json.loads(unit.projected_bytes.decode("utf-8"))
    return sha256_hex(canonical_project_bytes(obj, sort_keys=True))


def _span_for_unit(
    unit: OrderedUnit,
    projected_spans: dict[str, ByteSpan] | None,
) -> ByteSpan | None:
    if unit.wire_span is not None:
        return unit.wire_span
    if projected_spans is not None:
        return projected_spans.get(unit.json_path) or ByteSpan(
            space=OffsetSpace.PROJECTED,
            start=0,
            end=len(unit.projected_bytes),
        )
    return None


def _span_for_path(
    json_path: str,
    wire_spans: dict[str, ByteSpan] | None,
    projected_spans: dict[str, ByteSpan] | None,
) -> ByteSpan | None:
    if wire_spans is not None and json_path in wire_spans:
        return wire_spans[json_path]
    if projected_spans is not None and json_path in projected_spans:
        return projected_spans[json_path]
    return None


def _compare_configs(
    prev_cfg: CacheConfigFingerprint,
    curr_cfg: CacheConfigFingerprint,
    wire_spans: dict[str, ByteSpan] | None,
    projected_spans: dict[str, ByteSpan] | None,
    *,
    include_snippets: bool,
) -> list[ObservedChange]:
    changes: list[ObservedChange] = []

    def _emit_cfg(path: str, before_val: Any, after_val: Any) -> None:
        b_bytes = canonical_project_bytes(before_val) if before_val is not None else None
        a_bytes = canonical_project_bytes(after_val) if after_val is not None else None
        changes.append(
            ObservedChange(
                change_kind=ChangeKind.CONFIG_CHANGED,
                json_path=path,
                span=_span_for_path(path, wire_spans, projected_spans),
                before_len=len(b_bytes) if b_bytes is not None else None,
                after_len=len(a_bytes) if a_bytes is not None else None,
                before_sha256=sha256_hex(b_bytes) if b_bytes is not None else None,
                after_sha256=sha256_hex(a_bytes) if a_bytes is not None else None,
                before_snippet=_snippet_from_bytes(b_bytes, include=include_snippets),
                after_snippet=_snippet_from_bytes(a_bytes, include=include_snippets),
            )
        )

    if prev_cfg.model != curr_cfg.model:
        _emit_cfg("$.model", prev_cfg.model, curr_cfg.model)

    if not order_sensitive_equal(prev_cfg.reasoning_or_thinking, curr_cfg.reasoning_or_thinking):
        _emit_cfg("$.reasoning", prev_cfg.reasoning_or_thinking, curr_cfg.reasoning_or_thinking)

    if prev_cfg.output_schema_sha256 != curr_cfg.output_schema_sha256:
        changes.append(
            ObservedChange(
                change_kind=ChangeKind.CONFIG_CHANGED,
                json_path="$.output_schema",
                span=_span_for_path("$.text.format", wire_spans, projected_spans),
                before_len=None,
                after_len=None,
                before_sha256=prev_cfg.output_schema_sha256,
                after_sha256=curr_cfg.output_schema_sha256,
            )
        )

    if prev_cfg.service_tier != curr_cfg.service_tier:
        _emit_cfg("$.service_tier", prev_cfg.service_tier, curr_cfg.service_tier)

    if not order_sensitive_equal(prev_cfg.cache_params, curr_cfg.cache_params):
        prev_map = {path: (name, val) for path, name, val in prev_cfg.cache_params}
        curr_map = {path: (name, val) for path, name, val in curr_cfg.cache_params}
        seen_paths = list(dict.fromkeys([*prev_map.keys(), *curr_map.keys()]))
        for path in seen_paths:
            p_item = prev_map.get(path)
            c_item = curr_map.get(path)
            if p_item is None and c_item is not None:
                _emit_cfg(path, None, c_item[1])
            elif c_item is None and p_item is not None:
                _emit_cfg(path, p_item[1], None)
            elif (
                p_item is not None
                and c_item is not None
                and not order_sensitive_equal(p_item[1], c_item[1])
            ):
                _emit_cfg(path, p_item[1], c_item[1])

    return changes


def _compare_nonconv_group(
    prev_group: list[OrderedUnit],
    curr_group: list[OrderedUnit],
    curr_unit_index: dict[str, int],
    total_curr_units: int,
    projected_spans: dict[str, ByteSpan] | None,
    *,
    include_snippets: bool,
) -> list[ObservedChange]:
    changes: list[ObservedChange] = []
    prev_hashes = [u.projected_sha256 for u in prev_group]
    curr_hashes = [u.projected_sha256 for u in curr_group]
    if prev_hashes == curr_hashes:
        return changes

    # Pure permutation of existing units
    if len(prev_group) == len(curr_group) and Counter(prev_hashes) == Counter(curr_hashes):
        for prev_u, curr_u in zip(prev_group, curr_group, strict=True):
            if prev_u.projected_sha256 != curr_u.projected_sha256:
                changes.append(
                    ObservedChange(
                        change_kind=ChangeKind.REORDERED_EXISTING,
                        json_path=curr_u.json_path,
                        span=_span_for_unit(curr_u, projected_spans),
                        before_len=len(prev_u.projected_bytes),
                        after_len=len(curr_u.projected_bytes),
                        before_sha256=prev_u.projected_sha256,
                        after_sha256=curr_u.projected_sha256,
                        origin=curr_u.origin,
                        before_snippet=_snippet_from_bytes(
                            prev_u.projected_bytes, include=include_snippets
                        ),
                        after_snippet=_snippet_from_bytes(
                            curr_u.projected_bytes, include=include_snippets
                        ),
                    )
                )
        return changes

    common_len = min(len(prev_group), len(curr_group))
    prefix_match = 0
    while (
        prefix_match < common_len
        and prev_group[prefix_match].projected_sha256 == curr_group[prefix_match].projected_sha256
    ):
        prefix_match += 1

    # Shared prefix, remaining slots modified/inserted/deleted
    for idx in range(prefix_match, common_len):
        prev_u = prev_group[idx]
        curr_u = curr_group[idx]
        if prev_u.projected_sha256 == curr_u.projected_sha256:
            continue
        kind = (
            ChangeKind.REORDERED_EXISTING
            if _sorted_sha256(prev_u) == _sorted_sha256(curr_u)
            else ChangeKind.MUTATED_EXISTING
        )
        changes.append(
            ObservedChange(
                change_kind=kind,
                json_path=curr_u.json_path,
                span=_span_for_unit(curr_u, projected_spans),
                before_len=len(prev_u.projected_bytes),
                after_len=len(curr_u.projected_bytes),
                before_sha256=prev_u.projected_sha256,
                after_sha256=curr_u.projected_sha256,
                origin=curr_u.origin,
                before_snippet=_snippet_from_bytes(
                    prev_u.projected_bytes, include=include_snippets
                ),
                after_snippet=_snippet_from_bytes(curr_u.projected_bytes, include=include_snippets),
            )
        )

    for idx in range(common_len, len(curr_group)):
        curr_u = curr_group[idx]
        pos_in_curr = curr_unit_index.get(curr_u.json_path, total_curr_units - 1)
        has_downstream = pos_in_curr < total_curr_units - 1
        kind = ChangeKind.INSERTED_BEFORE_TAIL if has_downstream else ChangeKind.NORMAL_APPEND
        changes.append(
            ObservedChange(
                change_kind=kind,
                json_path=curr_u.json_path,
                span=_span_for_unit(curr_u, projected_spans),
                before_len=None,
                after_len=len(curr_u.projected_bytes),
                before_sha256=None,
                after_sha256=curr_u.projected_sha256,
                origin=curr_u.origin,
                before_snippet=None,
                after_snippet=_snippet_from_bytes(curr_u.projected_bytes, include=include_snippets),
            )
        )

    for idx in range(common_len, len(prev_group)):
        prev_u = prev_group[idx]
        changes.append(
            ObservedChange(
                change_kind=ChangeKind.DELETED_EXISTING,
                json_path=prev_u.json_path,
                span=None,
                before_len=len(prev_u.projected_bytes),
                after_len=None,
                before_sha256=prev_u.projected_sha256,
                after_sha256=None,
                origin=prev_u.origin,
                before_snippet=_snippet_from_bytes(
                    prev_u.projected_bytes, include=include_snippets
                ),
                after_snippet=None,
            )
        )

    return changes


def _compare_conv_units(
    prev_conv: list[OrderedUnit],
    curr_conv: list[OrderedUnit],
    projected_spans: dict[str, ByteSpan] | None,
    *,
    include_snippets: bool,
) -> list[ObservedChange]:
    changes: list[ObservedChange] = []
    if not prev_conv and not curr_conv:
        return changes

    if len(curr_conv) < len(prev_conv):
        # Single trailing drop -> DELETED_EXISTING; otherwise COMPACTED_OR_TRUNCATED
        is_tail_drop = len(prev_conv) - len(curr_conv) == 1 and all(
            p.projected_sha256 == c.projected_sha256
            for p, c in zip(prev_conv[: len(curr_conv)], curr_conv, strict=True)
        )
        if is_tail_drop:
            dropped = prev_conv[-1]
            changes.append(
                ObservedChange(
                    change_kind=ChangeKind.DELETED_EXISTING,
                    json_path=dropped.json_path,
                    span=None,
                    before_len=len(dropped.projected_bytes),
                    after_len=None,
                    before_sha256=dropped.projected_sha256,
                    after_sha256=None,
                    origin=dropped.origin,
                    before_snippet=_snippet_from_bytes(
                        dropped.projected_bytes, include=include_snippets
                    ),
                    after_snippet=None,
                )
            )
            return changes

        target = curr_conv[0] if curr_conv else prev_conv[0]
        changes.append(
            ObservedChange(
                change_kind=ChangeKind.COMPACTED_OR_TRUNCATED,
                json_path=target.json_path,
                span=_span_for_unit(target, projected_spans) if curr_conv else None,
                before_len=sum(len(u.projected_bytes) for u in prev_conv),
                after_len=sum(len(u.projected_bytes) for u in curr_conv),
                before_sha256=prev_conv[0].projected_sha256,
                after_sha256=curr_conv[0].projected_sha256 if curr_conv else None,
                origin=target.origin,
                before_snippet=_snippet_from_bytes(
                    prev_conv[0].projected_bytes, include=include_snippets
                ),
                after_snippet=(
                    _snippet_from_bytes(curr_conv[0].projected_bytes, include=include_snippets)
                    if curr_conv
                    else None
                ),
            )
        )
        return changes

    common_len = len(prev_conv)
    for idx in range(common_len):
        prev_u = prev_conv[idx]
        curr_u = curr_conv[idx]
        if prev_u.projected_sha256 == curr_u.projected_sha256:
            continue
        kind = (
            ChangeKind.REORDERED_EXISTING
            if _sorted_sha256(prev_u) == _sorted_sha256(curr_u)
            else ChangeKind.MUTATED_EXISTING
        )
        changes.append(
            ObservedChange(
                change_kind=kind,
                json_path=curr_u.json_path,
                span=_span_for_unit(curr_u, projected_spans),
                before_len=len(prev_u.projected_bytes),
                after_len=len(curr_u.projected_bytes),
                before_sha256=prev_u.projected_sha256,
                after_sha256=curr_u.projected_sha256,
                origin=curr_u.origin,
                before_snippet=_snippet_from_bytes(
                    prev_u.projected_bytes, include=include_snippets
                ),
                after_snippet=_snippet_from_bytes(curr_u.projected_bytes, include=include_snippets),
            )
        )

    for idx in range(common_len, len(curr_conv)):
        curr_u = curr_conv[idx]
        changes.append(
            ObservedChange(
                change_kind=ChangeKind.NORMAL_APPEND,
                json_path=curr_u.json_path,
                span=_span_for_unit(curr_u, projected_spans),
                before_len=None,
                after_len=len(curr_u.projected_bytes),
                before_sha256=None,
                after_sha256=curr_u.projected_sha256,
                origin=curr_u.origin,
                before_snippet=None,
                after_snippet=_snippet_from_bytes(curr_u.projected_bytes, include=include_snippets),
            )
        )

    return changes


def compare_turns(
    prev_record: CaptureRecord,
    curr_record: CaptureRecord,
    prev_units: tuple[OrderedUnit, ...],
    curr_units: tuple[OrderedUnit, ...],
    prev_config: CacheConfigFingerprint,
    curr_config: CacheConfigFingerprint,
    *,
    include_snippets: bool = False,
) -> tuple[ObservedChange, ...]:
    """Compare two consecutive turns, separating wire formatting from prefix mutations."""
    if (
        prev_record.captured_body_bytes is not None
        and curr_record.captured_body_bytes is not None
        and order_sensitive_equal(prev_record.parsed_body, curr_record.parsed_body)
        and order_sensitive_equal(prev_config, curr_config)
    ):
        prev_raw = prev_record.captured_body_bytes
        curr_raw = curr_record.captured_body_bytes
        if prev_raw == curr_raw:
            return ()
        return (
            ObservedChange(
                change_kind=ChangeKind.WIRE_ONLY_DIFF,
                json_path="$",
                span=ByteSpan(OffsetSpace.WIRE_RAW, 0, len(curr_raw)),
                before_len=len(prev_raw),
                after_len=len(curr_raw),
                before_sha256=sha256_hex(prev_raw),
                after_sha256=sha256_hex(curr_raw),
                before_snippet=_snippet_from_bytes(prev_raw, include=include_snippets),
                after_snippet=_snippet_from_bytes(curr_raw, include=include_snippets),
            ),
        )

    wire_spans = (
        scan_json_spans(curr_record.captured_body_bytes, space=OffsetSpace.WIRE_RAW)
        if curr_record.captured_body_bytes is not None
        else None
    )
    projected_spans = (
        scan_json_spans(
            canonical_project_bytes(curr_record.parsed_body),
            space=OffsetSpace.PROJECTED,
        )
        if curr_record.captured_body_bytes is None
        else None
    )

    changes: list[ObservedChange] = []
    changes.extend(
        _compare_configs(
            prev_config,
            curr_config,
            wire_spans,
            projected_spans,
            include_snippets=include_snippets,
        )
    )

    curr_unit_index = {u.json_path: i for i, u in enumerate(curr_units)}

    # Partition non-conversation units by role while preserving relative order per role
    for role in (DisplayRole.TOOLS, DisplayRole.SYSTEM_OR_INSTRUCTIONS, DisplayRole.UNKNOWN):
        prev_group = [u for u in prev_units if u.display_role is role]
        curr_group = [u for u in curr_units if u.display_role is role]
        if prev_group or curr_group:
            changes.extend(
                _compare_nonconv_group(
                    prev_group,
                    curr_group,
                    curr_unit_index,
                    len(curr_units),
                    projected_spans,
                    include_snippets=include_snippets,
                )
            )

    prev_conv = [u for u in prev_units if u.display_role in _CONV_ROLES]
    curr_conv = [u for u in curr_units if u.display_role in _CONV_ROLES]
    changes.extend(
        _compare_conv_units(
            prev_conv,
            curr_conv,
            projected_spans,
            include_snippets=include_snippets,
        )
    )

    return tuple(changes)
