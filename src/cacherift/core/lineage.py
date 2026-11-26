"""Four-tier session lineage resolution separate from prompt_cache_key."""

from __future__ import annotations

from dataclasses import dataclass, field

from cacherift.core.models import (
    CaptureRecord,
    DisplayRole,
    LineageMatch,
    LineageStatus,
    OrderedUnit,
)

_CONV_ROLES = frozenset({DisplayRole.HISTORY_MESSAGE, DisplayRole.CURRENT_INPUT})


@dataclass(slots=True)
class _TrieNode:
    children: dict[str, _TrieNode] = field(default_factory=dict)
    terminal_ids: list[str] = field(default_factory=list)
    passing_ids: list[str] = field(default_factory=list)


class PrefixTrie:
    """Trie over unit projected SHA-256 sequences for fast heuristic prefix lookup."""

    def __init__(self) -> None:
        self._root = _TrieNode()

    def insert(self, record_id: str, hashes: tuple[str, ...]) -> None:
        node = self._root
        for unit_hash in hashes:
            node = node.children.setdefault(unit_hash, _TrieNode())
            node.passing_ids.append(record_id)
        node.terminal_ids.append(record_id)

    def longest_shared_prefix_candidates(
        self,
        hashes: tuple[str, ...],
        eligible_ids: set[str],
    ) -> tuple[int, list[str]]:
        """Return (prefix_len, matching_record_ids) for longest shared non-empty prefix."""
        node = self._root
        best_len = 0
        best_ids: list[str] = []
        for depth, unit_hash in enumerate(hashes, start=1):
            nxt = node.children.get(unit_hash)
            if nxt is None:
                break
            hits = [rid for rid in nxt.passing_ids if rid in eligible_ids]
            if not hits:
                break
            best_len = depth
            best_ids = hits
            node = nxt
        return best_len, best_ids


@dataclass(frozen=True, slots=True)
class _RegisteredEntry:
    record: CaptureRecord
    unit_hashes: tuple[str, ...]
    conv_hashes: tuple[str, ...]
    session_key: str


class LineageResolver:
    """Resolve turn parent lineage strictly via 4-tier precedence and preserve ambiguity."""

    def __init__(self) -> None:
        self._entries: dict[str, _RegisteredEntry] = {}
        self._insertion_order: list[str] = []
        self._by_session: dict[str, list[str]] = {}
        self._by_response_id: dict[str, str] = {}
        self._authoritative_superseded: set[str] = set()
        self._trie = PrefixTrie()

    def resolve_and_register(
        self,
        record: CaptureRecord,
        units: tuple[OrderedUnit, ...],
        prev_response_ref: str | None,
    ) -> tuple[LineageMatch, CaptureRecord | None]:
        """Resolve parent record for ``record`` and register ``record`` for future turns."""
        unit_hashes = tuple(u.projected_sha256 for u in units)
        conv_hashes = tuple(u.projected_sha256 for u in units if u.display_role in _CONV_ROLES)

        match = self._resolve(record, unit_hashes, conv_hashes, prev_response_ref)
        parent_record = (
            self._entries[match.parent_record_id].record
            if match.parent_record_id is not None
            else None
        )
        self._register(record, unit_hashes, conv_hashes, match)
        return match, parent_record

    def _resolve(
        self,
        record: CaptureRecord,
        unit_hashes: tuple[str, ...],
        conv_hashes: tuple[str, ...],
        prev_response_ref: str | None,
    ) -> LineageMatch:
        # Tier 1: Explicit client metadata (parent_record_id, then client_session_id)
        if record.parent_record_id is not None and record.parent_record_id in self._entries:
            parent_id = record.parent_record_id
            session_key = (
                f"client:{record.client_session_id}"
                if record.client_session_id
                else self._entries[parent_id].session_key
            )
            return LineageMatch(
                status=LineageStatus.MATCHED,
                session_key=session_key,
                parent_record_id=parent_id,
                candidate_parent_ids=(parent_id,),
                match_basis="explicit_metadata",
            )

        if record.client_session_id:
            session_key = f"client:{record.client_session_id}"
            prior_ids = self._by_session.get(record.client_session_id, ())
            if prior_ids:
                best_id = max(
                    prior_ids,
                    key=lambda rid: (
                        self._entries[rid].record.timestamp,
                        self._insertion_order.index(rid),
                    ),
                )
                return LineageMatch(
                    status=LineageStatus.MATCHED,
                    session_key=session_key,
                    parent_record_id=best_id,
                    candidate_parent_ids=(best_id,),
                    match_basis="explicit_metadata",
                )
            return LineageMatch(
                status=LineageStatus.NO_BASELINE,
                session_key=session_key,
                parent_record_id=None,
                candidate_parent_ids=(),
                match_basis="none",
            )

        # Tier 2: Response ID linkage chain
        if prev_response_ref and prev_response_ref in self._by_response_id:
            parent_id = self._by_response_id[prev_response_ref]
            return LineageMatch(
                status=LineageStatus.MATCHED,
                session_key=self._entries[parent_id].session_key,
                parent_record_id=parent_id,
                candidate_parent_ids=(parent_id,),
                match_basis="response_id_chain",
            )

        # Tier 3: Full conversation history continuation
        if conv_hashes:
            best_conv_len = 0
            conv_candidates: list[str] = []
            for rid in self._insertion_order:
                entry = self._entries[rid]
                if entry.record.identity.dialect != record.identity.dialect:
                    continue
                prev_conv = entry.conv_hashes
                if (
                    prev_conv
                    and len(conv_hashes) > len(prev_conv)
                    and conv_hashes[: len(prev_conv)] == prev_conv
                ):
                    if len(prev_conv) > best_conv_len:
                        best_conv_len = len(prev_conv)
                        conv_candidates = [rid]
                    elif len(prev_conv) == best_conv_len:
                        conv_candidates.append(rid)

            if len(conv_candidates) == 1:
                parent_id = conv_candidates[0]
                return LineageMatch(
                    status=LineageStatus.MATCHED,
                    session_key=self._entries[parent_id].session_key,
                    parent_record_id=parent_id,
                    candidate_parent_ids=(parent_id,),
                    match_basis="history_continuation",
                )
            if len(conv_candidates) > 1:
                return LineageMatch(
                    status=LineageStatus.AMBIGUOUS,
                    session_key=f"ambiguous:{record.record_id}",
                    parent_record_id=None,
                    candidate_parent_ids=tuple(conv_candidates),
                    match_basis="history_continuation",
                    note=(
                        "Multiple prior turns share an identical conversation "
                        "continuation prefix."
                    ),
                )

        # Tier 4: Heuristic Trie prefix match (skip twin starts & superseded ancestors)
        eligible_ids = {
            rid
            for rid in self._insertion_order
            if (
                self._entries[rid].record.identity.dialect == record.identity.dialect
                and rid not in self._authoritative_superseded
                and self._entries[rid].unit_hashes != unit_hashes
            )
        }
        prefix_len, trie_candidates = self._trie.longest_shared_prefix_candidates(
            unit_hashes,
            eligible_ids,
        )
        if prefix_len >= 1:
            if len(trie_candidates) == 1:
                parent_id = trie_candidates[0]
                return LineageMatch(
                    status=LineageStatus.MATCHED,
                    session_key=self._entries[parent_id].session_key,
                    parent_record_id=parent_id,
                    candidate_parent_ids=(parent_id,),
                    match_basis="heuristic_prefix",
                )
            return LineageMatch(
                status=LineageStatus.AMBIGUOUS,
                session_key=f"ambiguous:{record.record_id}",
                parent_record_id=None,
                candidate_parent_ids=tuple(trie_candidates),
                match_basis="heuristic_prefix",
                note="Multiple prior turns share the same longest heuristic unit prefix.",
            )

        return LineageMatch(
            status=LineageStatus.NO_BASELINE,
            session_key=f"record:{record.record_id}",
            parent_record_id=None,
            candidate_parent_ids=(),
            match_basis="none",
        )

    def _register(
        self,
        record: CaptureRecord,
        unit_hashes: tuple[str, ...],
        conv_hashes: tuple[str, ...],
        match: LineageMatch,
    ) -> None:
        entry = _RegisteredEntry(
            record=record,
            unit_hashes=unit_hashes,
            conv_hashes=conv_hashes,
            session_key=match.session_key,
        )
        self._entries[record.record_id] = entry
        self._insertion_order.append(record.record_id)
        self._trie.insert(record.record_id, unit_hashes)

        if record.client_session_id:
            self._by_session.setdefault(record.client_session_id, []).append(record.record_id)
        if record.response_id:
            self._by_response_id[record.response_id] = record.record_id
        if match.parent_record_id is not None and match.match_basis in (
            "explicit_metadata",
            "response_id_chain",
            "history_continuation",
        ):
            self._authoritative_superseded.add(match.parent_record_id)
