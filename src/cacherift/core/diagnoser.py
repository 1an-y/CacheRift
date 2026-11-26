"""Multi-evidence turn diagnoser separating observations, provider reports, and inferences."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cacherift.core.models import (
    CandidateCause,
    CapabilityInfo,
    CaptureRecord,
    ChangeKind,
    CostEstimate,
    DisplayRole,
    EvidenceGrade,
    LineageMatch,
    LineageStatus,
    ObservedChange,
    OrderedUnit,
    ProviderDiagnostics,
    TurnDiagnosis,
    UsageObservation,
    UsageState,
)

_BREAKING_CHANGE_KINDS = frozenset(
    {
        ChangeKind.MUTATED_EXISTING,
        ChangeKind.DELETED_EXISTING,
        ChangeKind.REORDERED_EXISTING,
        ChangeKind.COMPACTED_OR_TRUNCATED,
        ChangeKind.INSERTED_BEFORE_TAIL,
        ChangeKind.CONFIG_CHANGED,
    }
)


def _is_tail_only_edit(
    observed_changes: tuple[ObservedChange, ...],
    current_units: Sequence[OrderedUnit],
) -> bool:
    """True when the only breaking changes edit the final conversation unit of the request.

    Everything before that unit is byte-identical to the parent, so the shared prefix is intact.
    A turn without any breaking change is not a tail edit; it is simply unchanged.
    """
    if not current_units:
        return False
    tail_paths = {
        unit.json_path for unit in current_units if unit.display_role is DisplayRole.CURRENT_INPUT
    }
    tail_edits = 0
    for change in observed_changes:
        if change.change_kind not in _BREAKING_CHANGE_KINDS:
            continue
        if (
            change.change_kind is not ChangeKind.MUTATED_EXISTING
            or change.json_path not in tail_paths
        ):
            return False
        tail_edits += 1
    return tail_edits > 0


def _append_observed_change_causes(
    observed_changes: tuple[ObservedChange, ...],
    causes: list[CandidateCause],
) -> None:
    by_kind: dict[ChangeKind, list[str]] = {}
    for change in observed_changes:
        by_kind.setdefault(change.change_kind, []).append(change.json_path)

    kind_specs: tuple[tuple[ChangeKind, str, str], ...] = (
        (
            ChangeKind.MUTATED_EXISTING,
            "existing_prefix_mutated",
            "Existing prefix unit content changed relative to the parent turn.",
        ),
        (
            ChangeKind.DELETED_EXISTING,
            "existing_prefix_deleted",
            "An existing upstream unit from the parent turn was removed.",
        ),
        (
            ChangeKind.REORDERED_EXISTING,
            "existing_prefix_reordered",
            "Existing keys or array elements were reordered, altering tokenized prefix bytes.",
        ),
        (
            ChangeKind.COMPACTED_OR_TRUNCATED,
            "history_compacted_or_truncated",
            "Earlier conversation turns were compacted, summarized, or truncated.",
        ),
        (
            ChangeKind.INSERTED_BEFORE_TAIL,
            "upstream_layer_inserted",
            "New unit(s) were inserted upstream of downstream system/history layers.",
        ),
        (
            ChangeKind.CONFIG_CHANGED,
            "cache_config_changed",
            "Model, reasoning/thinking, output schema, service tier, or cache parameters changed.",
        ),
        (
            ChangeKind.WIRE_ONLY_DIFF,
            "wire_formatting_only",
            "Only wire JSON whitespace or Unicode escapes changed; parsed structure is unchanged.",
        ),
    )

    for kind, code, summary in kind_specs:
        paths = by_kind.get(kind)
        if paths:
            causes.append(
                CandidateCause(
                    code=code,
                    grade=EvidenceGrade.OBSERVED,
                    summary=summary,
                    supporting_evidence=tuple(paths),
                )
            )


def diagnose_turn(
    record: CaptureRecord,
    lineage: LineageMatch,
    parent_record: CaptureRecord | None,
    observed_changes: tuple[ObservedChange, ...],
    usage: UsageObservation,
    provider_diag: ProviderDiagnostics | None,
    capability: CapabilityInfo,
    cost_estimate: CostEstimate | None,
    *,
    parent_usage: UsageObservation | None = None,
    current_units: Sequence[OrderedUnit] = (),
) -> TurnDiagnosis:
    """Aggregate observed changes, usage buckets, provider diagnostics, and inferred hypotheses.

    ``current_units`` are the request's ordered units; when given, an edit confined to the final
    conversation unit is not treated as a prefix break. Without it, v0.1 behavior is kept.
    """
    elapsed = record.timestamp - parent_record.timestamp if parent_record is not None else None

    causes: list[CandidateCause] = []
    unresolved: list[str] = []

    if lineage.status is LineageStatus.NO_BASELINE:
        if usage.buckets is not None and usage.buckets.cache_read > 0:
            causes.append(
                CandidateCause(
                    code="warm_cache_without_local_baseline",
                    grade=EvidenceGrade.OBSERVED,
                    summary=(
                        "First locally captured turn already reports positive cache_read; "
                        "upstream cache state pre-dated this capture."
                    ),
                    supporting_evidence=(f"cache_read={usage.buckets.cache_read}",),
                )
            )
        unresolved.append(
            "No local parent baseline captured before this turn; "
            "prior prefix mutations cannot be checked locally."
        )
    elif lineage.status is LineageStatus.AMBIGUOUS:
        causes.append(
            CandidateCause(
                code="ambiguous_lineage",
                grade=EvidenceGrade.UNRESOLVED,
                summary=(
                    "Multiple candidate parent turns share an identical prefix "
                    "without disambiguating metadata."
                ),
                supporting_evidence=lineage.candidate_parent_ids,
            )
        )
        unresolved.append(
            "Lineage is ambiguous across candidate parents: "
            + ", ".join(lineage.candidate_parent_ids)
        )

    _append_observed_change_causes(observed_changes, causes)

    has_breaking_change = any(c.change_kind in _BREAKING_CHANGE_KINDS for c in observed_changes)
    tail_only_edit = _is_tail_only_edit(observed_changes, current_units)

    if usage.state is not UsageState.COMPLETE or usage.buckets is None:
        unresolved.append(
            f"Usage observation is {usage.state.value} ({', '.join(usage.notes) or 'no_details'})."
        )
    else:
        buckets = usage.buckets
        if buckets.cache_read > 0 and has_breaking_change:
            causes.append(
                CandidateCause(
                    code="partial_prefix_cache_hit",
                    grade=EvidenceGrade.OBSERVED,
                    summary=(
                        "Earlier prefix bytes achieved cache_read > 0 concurrently with "
                        "changes in later or appended portions of the request."
                    ),
                    supporting_evidence=(
                        f"cache_read={buckets.cache_read}",
                        f"total_input={buckets.total_input}",
                    ),
                )
            )

        if (
            buckets.cache_read == 0
            and lineage.status is LineageStatus.MATCHED
            and (not has_breaking_change or tail_only_edit)
        ):
            parent_zero = (
                parent_usage is not None
                and parent_usage.buckets is not None
                and parent_usage.buckets.cache_read == 0
            )
            ttl_expired = (
                elapsed is not None
                and capability.ttl_seconds_hint is not None
                and elapsed > capability.ttl_seconds_hint
            )
            if not ttl_expired:
                ev: list[str] = [
                    f"curr_cache_read={buckets.cache_read}",
                    f"total_input={buckets.total_input}",
                ]
                if parent_zero:
                    ev.append("parent_cache_read=0")
                if tail_only_edit:
                    ev.append(
                        "tail_edit="
                        + ",".join(
                            c.json_path
                            for c in observed_changes
                            if c.change_kind is ChangeKind.MUTATED_EXISTING
                        )
                    )
                causes.append(
                    CandidateCause(
                        code="unexplained_low_hit",
                        grade=EvidenceGrade.INFERRED,
                        summary=(
                            "Only the final conversation unit changed and the shared prefix "
                            "before it stayed intact without exceeding TTL hint, "
                            "yet cache_read remained 0."
                            if tail_only_edit
                            else "Shared prefix remained structurally unchanged without exceeding "
                            "TTL hint, yet cache_read remained 0."
                        ),
                        supporting_evidence=tuple(ev),
                    )
                )
                if (
                    capability.min_cache_tokens is not None
                    and buckets.total_input < capability.min_cache_tokens
                ):
                    causes.append(
                        CandidateCause(
                            code="possible_below_min_cache_tokens",
                            grade=EvidenceGrade.INFERRED,
                            summary=(
                                "Total input tokens are below the registered "
                                "minimum cache threshold."
                            ),
                            supporting_evidence=(
                                f"total_input={buckets.total_input}",
                                f"min_cache_tokens={capability.min_cache_tokens}",
                            ),
                        )
                    )
                else:
                    causes.append(
                        CandidateCause(
                            code="possible_provider_routing_or_eviction",
                            grade=EvidenceGrade.INFERRED,
                            summary=(
                                "Server-side KV cache eviction or cache-shard routing miss "
                                "may have occurred."
                            ),
                            supporting_evidence=tuple(ev),
                        )
                    )
                causes.append(
                    CandidateCause(
                        code="possible_gateway_param_forwarding_issue",
                        grade=EvidenceGrade.INFERRED,
                        summary=(
                            "An intermediate gateway/proxy could be altering ordering or dropping "
                            "cache hints; cannot be proven without upstream wire inspection."
                        ),
                        supporting_evidence=tuple(ev),
                    )
                )
                unresolved.append(
                    "Zero cache_read with structurally stable prefix cannot distinguish provider "
                    "eviction/routing from upstream gateway forwarding behavior alone."
                )

    if (
        elapsed is not None
        and capability.ttl_seconds_hint is not None
        and elapsed > capability.ttl_seconds_hint
        and (
            usage.buckets is None
            or usage.buckets.cache_read == 0
            or usage.buckets.reported_hit_ratio < 1.0
        )
    ):
        causes.append(
            CandidateCause(
                code="possible_ttl_expiry",
                grade=EvidenceGrade.INFERRED,
                summary=(
                    f"Elapsed time since parent turn ({elapsed:.1f}s) exceeds TTL hint "
                    f"({capability.ttl_seconds_hint}s)."
                ),
                supporting_evidence=(
                    f"elapsed_seconds={elapsed:.1f}",
                    f"ttl_seconds_hint={capability.ttl_seconds_hint}",
                ),
            )
        )

    if (
        record.identity.dialect == "openai_responses"
        and record.identity.provider == "openai"
        and capability.unit_order == ("tools", "input", "instructions")
        and isinstance(record.parsed_body, Mapping)
        and record.parsed_body.get("instructions")
    ):
        causes.append(
            CandidateCause(
                code="non_cacheable_top_level_instructions",
                grade=EvidenceGrade.INFERRED,
                summary=(
                    "Top-level `instructions` in this model generation is not cached across turns; "
                    "move reusable instructions into a leading developer message in `input`."
                ),
                supporting_evidence=("$.instructions",),
            )
        )

    if provider_diag is not None:
        ev_diag = list(provider_diag.reported_divergences)
        if provider_diag.compared_response_id:
            ev_diag.append(f"compared_response_id={provider_diag.compared_response_id}")
        if provider_diag.caveat:
            ev_diag.append(f"caveat={provider_diag.caveat}")
        causes.append(
            CandidateCause(
                code="provider_reported_diagnostics",
                grade=EvidenceGrade.PROVIDER_REPORTED,
                summary=f"Upstream {provider_diag.source} reported diagnostic metadata.",
                supporting_evidence=tuple(ev_diag),
            )
        )

    if capability.support_level == "unknown":
        unresolved.append(
            f"Model capability metadata for ({record.identity.dialect}, "
            f"{record.identity.provider}, {record.identity.model}) is unregistered; "
            "min_cache_tokens and TTL semantics are unknown."
        )

    return TurnDiagnosis(
        record_id=record.record_id,
        identity=record.identity,
        lineage=lineage,
        capability=capability,
        observed_changes=observed_changes,
        usage=usage,
        provider_diagnostics=provider_diag,
        cost_estimate=cost_estimate,
        candidate_causes=tuple(causes),
        unresolved=tuple(unresolved),
        elapsed_since_parent_seconds=elapsed,
    )
