"""Immutable domain models and evidence grading types for CacheRift."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal


class OffsetSpace(StrEnum):
    WIRE_RAW = "wire_raw"
    PROJECTED = "projected"


class DisplayRole(StrEnum):
    """Display grouping label; never assumed to dictate physical prefix order."""

    TOOLS = "tools"
    SYSTEM_OR_INSTRUCTIONS = "system_or_instructions"
    HISTORY_MESSAGE = "history_message"
    CURRENT_INPUT = "current_input"
    CACHE_MARKER = "cache_marker"
    UNKNOWN = "unknown"


class ChangeKind(StrEnum):
    WIRE_ONLY_DIFF = "wire_only_diff"
    NORMAL_APPEND = "normal_append"
    MUTATED_EXISTING = "mutated_existing"
    DELETED_EXISTING = "deleted_existing"
    REORDERED_EXISTING = "reordered_existing"
    COMPACTED_OR_TRUNCATED = "compacted_or_truncated"
    INSERTED_BEFORE_TAIL = "inserted_before_tail"
    CONFIG_CHANGED = "config_changed"


class EvidenceGrade(StrEnum):
    OBSERVED = "observed"
    PROVIDER_REPORTED = "provider_reported"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


class UsageState(StrEnum):
    COMPLETE = "complete"
    MISSING = "missing"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class LineageStatus(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NO_BASELINE = "no_baseline"


DialectName = Literal[
    "openai_responses",
    "anthropic_messages",
    "openai_chat",
    "deepseek_chat",
    "gemini_generate_content",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ByteSpan:
    space: OffsetSpace
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class EndpointIdentity:
    """Decouples protocol dialect, provider identity, model, and gateway profile."""

    dialect: DialectName
    provider: str
    model: str
    endpoint_path: str
    gateway_profile: str | None = None
    pricing_table_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityInfo:
    """Versioned provider/model cache capability metadata; all fields allow unknown."""

    min_cache_tokens: int | None
    ttl_semantics: str | None
    ttl_seconds_hint: int | None
    prefix_order_note: str | None
    support_level: Literal["full", "usage_only", "unknown"]
    source_version: str | None = None
    unit_order: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    """Frozen capture contract distinguishing raw HTTP body bytes from parsed payloads."""

    record_id: str
    timestamp: float
    identity: EndpointIdentity
    request_headers: MappingProxyType[str, str]
    captured_body_bytes: bytes | None
    parsed_body: Any
    response_id: str | None = None
    response_body: Any = None
    response_complete: bool = True
    client_session_id: str | None = None
    client_trace_id: str | None = None
    parent_record_id: str | None = None
    origin_map: MappingProxyType[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class OrderedUnit:
    """Input unit projected in the dialect's and model generation's cache evaluation order."""

    unit_id: str
    json_path: str
    display_role: DisplayRole
    projected_bytes: bytes
    projected_sha256: str
    wire_span: ByteSpan | None
    estimated_tokens: int | None = None
    token_estimator: str = "heuristic_bytes_div_4"
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class CacheConfigFingerprint:
    """Non-body configuration fields that govern server-side cache eligibility."""

    model: str
    reasoning_or_thinking: Any
    output_schema_sha256: str | None
    service_tier: str | None
    cache_params: tuple[tuple[str, str, Any], ...]


@dataclass(frozen=True, slots=True)
class CacheWriteBucket:
    tier: str
    tokens: int


@dataclass(frozen=True, slots=True)
class TokenBuckets:
    """Non-overlapping token buckets preventing cross-provider denominator errors."""

    uncached_input: int
    cache_read: int
    cache_writes: tuple[CacheWriteBucket, ...]
    output: int
    total_input: int
    reported_hit_ratio: float


@dataclass(frozen=True, slots=True)
class UsageObservation:
    state: UsageState
    buckets: TokenBuckets | None
    raw_fields: MappingProxyType[str, Any]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Estimated cost using an explicit versioned pricing table (never a billing claim)."""

    pricing_table_id: str
    estimated_input_cost_usd: float
    estimated_output_cost_usd: float
    estimated_total_cost_usd: float
    uncached_baseline_input_cost_usd: float
    estimated_input_delta_vs_uncached_usd: float


@dataclass(frozen=True, slots=True)
class ProviderDiagnostics:
    """Provider-native cache diagnostics reported by the upstream API."""

    source: str
    compared_response_id: str | None
    reported_divergences: tuple[str, ...]
    raw_payload: Any
    caveat: str | None = None


@dataclass(frozen=True, slots=True)
class LineageMatch:
    status: LineageStatus
    session_key: str
    parent_record_id: str | None
    candidate_parent_ids: tuple[str, ...]
    match_basis: Literal[
        "explicit_metadata",
        "response_id_chain",
        "history_continuation",
        "heuristic_prefix",
        "none",
    ]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedChange:
    """Verifiable diff entry; text snippets stay None unless explicitly requested."""

    change_kind: ChangeKind
    json_path: str
    span: ByteSpan | None
    before_len: int | None
    after_len: int | None
    before_sha256: str | None
    after_sha256: str | None
    origin: str | None = None
    before_snippet: str | None = None
    after_snippet: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateCause:
    code: str
    grade: EvidenceGrade
    summary: str
    supporting_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TurnDiagnosis:
    """Multi-evidence turn report keeping facts, diagnostics, and inferences distinct."""

    record_id: str
    identity: EndpointIdentity
    lineage: LineageMatch
    capability: CapabilityInfo
    observed_changes: tuple[ObservedChange, ...]
    usage: UsageObservation
    provider_diagnostics: ProviderDiagnostics | None
    cost_estimate: CostEstimate | None
    candidate_causes: tuple[CandidateCause, ...]
    unresolved: tuple[str, ...]
    elapsed_since_parent_seconds: float | None = None
