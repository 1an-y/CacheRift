from __future__ import annotations

from cacherift.capabilities.registry import CapabilityRegistry
from cacherift.core.freeze import freeze_json
from cacherift.core.lineage import LineageResolver
from cacherift.core.models import (
    CaptureRecord,
    EndpointIdentity,
    LineageStatus,
)
from cacherift.dialects.base import get_dialect_adapter


def _make_oai_record(
    record_id: str,
    timestamp: float,
    input_items: list[dict[str, str]],
    *,
    system: str = "You are helpful.",
    response_id: str | None = None,
    previous_response_id: str | None = None,
    client_session_id: str | None = None,
    parent_record_id: str | None = None,
    prompt_cache_key: str | None = None,
) -> CaptureRecord:
    body: dict[str, object] = {
        "model": "gpt-6.1",
        "instructions": system,
        "input": input_items,
    }
    if previous_response_id is not None:
        body["previous_response_id"] = previous_response_id
    if prompt_cache_key is not None:
        body["prompt_cache_key"] = prompt_cache_key
    return CaptureRecord(
        record_id=record_id,
        timestamp=timestamp,
        identity=EndpointIdentity("openai_responses", "openai", "gpt-6.1", "/v1/responses"),
        request_headers=freeze_json({}),
        captured_body_bytes=None,
        parsed_body=freeze_json(body),
        response_id=response_id,
        client_session_id=client_session_id,
        parent_record_id=parent_record_id,
    )


def _extract(rec: CaptureRecord) -> tuple[tuple, str | None]:  # type: ignore[type-arg]
    reg = CapabilityRegistry.load_default()
    cap = reg.lookup(rec.identity.dialect, rec.identity.provider, rec.identity.model)
    adapter = get_dialect_adapter(rec.identity.dialect)
    units, _ = adapter.extract_ordered_units(rec, cap)
    return units, adapter.extract_previous_response_ref(rec)


def test_lineage_tier1_explicit_metadata_and_c3_no_baseline() -> None:
    resolver = LineageResolver()

    r1 = _make_oai_record("r1", 10.0, [{"role": "user", "content": "hi"}], client_session_id="s1")
    u1, ref1 = _extract(r1)
    m1, p1 = resolver.resolve_and_register(r1, u1, ref1)
    assert m1.status is LineageStatus.NO_BASELINE
    assert m1.parent_record_id is None
    assert p1 is None

    # Even if instructions changed completely, client_session_id binds r2 -> r1
    r2 = _make_oai_record(
        "r2",
        20.0,
        [{"role": "user", "content": "unrelated"}],
        system="Completely different instructions.",
        client_session_id="s1",
    )
    u2, ref2 = _extract(r2)
    m2, p2 = resolver.resolve_and_register(r2, u2, ref2)
    assert m2.status is LineageStatus.MATCHED
    assert m2.match_basis == "explicit_metadata"
    assert m2.parent_record_id == "r1"
    assert p2 is not None and p2.record_id == "r1"


def test_lineage_tier2_response_id_chain_beats_history() -> None:
    resolver = LineageResolver()
    r1 = _make_oai_record("r1", 10.0, [{"role": "user", "content": "hi"}], response_id="resp_a")
    u1, ref1 = _extract(r1)
    resolver.resolve_and_register(r1, u1, ref1)

    r2 = _make_oai_record(
        "r2",
        20.0,
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "next"},
        ],
        previous_response_id="resp_a",
    )
    u2, ref2 = _extract(r2)
    m2, p2 = resolver.resolve_and_register(r2, u2, ref2)
    assert m2.status is LineageStatus.MATCHED
    assert m2.match_basis == "response_id_chain"
    assert m2.parent_record_id == "r1"
    assert p2 is not None and p2.record_id == "r1"


def test_lineage_tier3_history_continuation_survives_system_mutation() -> None:
    resolver = LineageResolver()
    r1 = _make_oai_record("r1", 10.0, [{"role": "user", "content": "hello"}])
    u1, ref1 = _extract(r1)
    resolver.resolve_and_register(r1, u1, ref1)

    r2 = _make_oai_record(
        "r2",
        20.0,
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
            {"role": "user", "content": "follow-up"},
        ],
        system="Modified system prompt!",
    )
    u2, ref2 = _extract(r2)
    m2, p2 = resolver.resolve_and_register(r2, u2, ref2)
    assert m2.status is LineageStatus.MATCHED
    assert m2.match_basis == "history_continuation"
    assert m2.parent_record_id == "r1"
    assert p2 is not None and p2.record_id == "r1"


def test_lineage_c4_ambiguous_identical_prefix_and_ignores_prompt_cache_key() -> None:
    resolver = LineageResolver()

    # Two independent terminals share the exact same prefix and same prompt_cache_key
    term_a = _make_oai_record(
        "term-a",
        10.0,
        [{"role": "user", "content": "start task"}],
        prompt_cache_key="shared-org-key",
    )
    term_b = _make_oai_record(
        "term-b",
        11.0,
        [{"role": "user", "content": "start task"}],
        prompt_cache_key="shared-org-key",
    )
    ua, refa = _extract(term_a)
    ub, refb = _extract(term_b)

    ma, _ = resolver.resolve_and_register(term_a, ua, refa)
    mb, _ = resolver.resolve_and_register(term_b, ub, refb)
    assert ma.status is LineageStatus.NO_BASELINE
    # Exact identical prefix without growth/metadata cannot deterministically claim term-a
    assert mb.status is LineageStatus.NO_BASELINE

    # Next turn continues the shared prefix without explicit metadata -> AMBIGUOUS (C4)
    follow = _make_oai_record(
        "follow",
        15.0,
        [
            {"role": "user", "content": "start task"},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": "step 2"},
        ],
        prompt_cache_key="shared-org-key",
    )
    uf, reff = _extract(follow)
    mf, pf = resolver.resolve_and_register(follow, uf, reff)
    assert mf.status is LineageStatus.AMBIGUOUS
    assert mf.parent_record_id is None
    assert pf is None
    assert set(mf.candidate_parent_ids) == {"term-a", "term-b"}

    # Different prompt sharing only prompt_cache_key must NOT link to term-a or term-b
    other = _make_oai_record(
        "other",
        20.0,
        [{"role": "user", "content": "totally unrelated topic"}],
        system="Different system prompt",
        prompt_cache_key="shared-org-key",
    )
    uo, refo = _extract(other)
    mo, po = resolver.resolve_and_register(other, uo, refo)
    assert mo.status is LineageStatus.NO_BASELINE
    assert po is None
