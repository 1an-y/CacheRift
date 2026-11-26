"""CLI entrypoint (`cacherift analyze`) and offline diagnosis pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from rich.console import Console
from rich.table import Table

from cacherift import __version__
from cacherift.capabilities.registry import CapabilityRegistry, PricingRegistry
from cacherift.core.diagnoser import diagnose_turn
from cacherift.core.diff import compare_turns
from cacherift.core.lineage import LineageResolver
from cacherift.core.models import (
    CacheConfigFingerprint,
    CaptureRecord,
    EndpointIdentity,
    ObservedChange,
    OrderedUnit,
    TurnDiagnosis,
    UsageObservation,
)
from cacherift.core.pricing import PricingTable, estimate_cost
from cacherift.dialects.base import get_dialect_adapter
from cacherift.io.capture import load_capture_records


def _to_json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_json_safe(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, MappingProxyType):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def analyze_records(
    records: Sequence[CaptureRecord],
    *,
    capability_registry: CapabilityRegistry | None = None,
    pricing_registry: PricingRegistry | None = None,
    pricing_table_id: str | None = None,
    include_snippets: bool = False,
) -> list[TurnDiagnosis]:
    """Run the full offline CacheRift diagnostic pipeline across a sequence of records."""
    cap_reg = capability_registry or CapabilityRegistry.load_default()
    price_reg = pricing_registry or PricingRegistry.load_default()

    resolver = LineageResolver()
    units_by_id: dict[str, tuple[OrderedUnit, ...]] = {}
    cfg_by_id: dict[str, CacheConfigFingerprint] = {}
    usage_by_id: dict[str, UsageObservation] = {}
    diagnoses: list[TurnDiagnosis] = []

    for rec in records:
        cap = cap_reg.lookup(
            rec.identity.dialect,
            rec.identity.provider,
            rec.identity.model,
            rec.identity.gateway_profile,
        )
        adapter = get_dialect_adapter(rec.identity.dialect)
        units, cfg = adapter.extract_ordered_units(rec, cap)
        prev_ref = adapter.extract_previous_response_ref(rec)
        lineage, parent = resolver.resolve_and_register(rec, units, prev_ref)
        usage = adapter.extract_usage(rec)
        provider_diag = adapter.extract_provider_diagnostics(rec)

        changes: tuple[ObservedChange, ...] = ()
        parent_usage: UsageObservation | None = None
        if parent is not None:
            changes = compare_turns(
                parent,
                rec,
                units_by_id[parent.record_id],
                units,
                cfg_by_id[parent.record_id],
                cfg,
                include_snippets=include_snippets,
            )
            parent_usage = usage_by_id[parent.record_id]

        effective_table_id = pricing_table_id or rec.identity.pricing_table_id
        table: PricingTable | None = (
            price_reg.get_table(effective_table_id) if effective_table_id is not None else None
        )
        effective_identity = (
            EndpointIdentity(
                dialect=rec.identity.dialect,
                provider=rec.identity.provider,
                model=rec.identity.model,
                endpoint_path=rec.identity.endpoint_path,
                gateway_profile=rec.identity.gateway_profile,
                pricing_table_id=effective_table_id,
            )
            if effective_table_id != rec.identity.pricing_table_id
            else rec.identity
        )
        cost = estimate_cost(effective_identity, usage, table)

        diag = diagnose_turn(
            rec,
            lineage,
            parent,
            changes,
            usage,
            provider_diag,
            cap,
            cost,
            parent_usage=parent_usage,
        )
        units_by_id[rec.record_id] = units
        cfg_by_id[rec.record_id] = cfg
        usage_by_id[rec.record_id] = usage
        diagnoses.append(diag)

    return diagnoses


def _render_table(diagnoses: Sequence[TurnDiagnosis], console: Console) -> None:
    table = Table(title="CacheRift Offline Diagnosis", show_lines=True)
    table.add_column("Record", style="bold")
    table.add_column("Dialect / Model")
    table.add_column("Lineage")
    table.add_column("Usage / Hit")
    table.add_column("Observed Changes")
    table.add_column("Candidate Causes / Unresolved")

    for d in diagnoses:
        lineage_txt = d.lineage.status.value
        if d.lineage.parent_record_id:
            lineage_txt += f"\n<- {d.lineage.parent_record_id} ({d.lineage.match_basis})"
        elif d.lineage.candidate_parent_ids:
            lineage_txt += "\n[" + ", ".join(d.lineage.candidate_parent_ids) + "]"

        if d.usage.buckets is not None:
            b = d.usage.buckets
            usage_txt = (
                f"{b.reported_hit_ratio * 100:.1f}%\n"
                f"read={b.cache_read} / total={b.total_input}\n"
                f"uncached={b.uncached_input} out={b.output}"
            )
        else:
            usage_txt = f"{d.usage.state.value}\n({', '.join(d.usage.notes)})"

        if d.cost_estimate is not None:
            usage_txt += f"\nest=${d.cost_estimate.estimated_total_cost_usd:.6f}"

        if d.observed_changes:
            chg_lines = []
            for ch in d.observed_changes:
                span_txt = (
                    f"{ch.span.space.value}[{ch.span.start}:{ch.span.end}]"
                    if ch.span is not None
                    else "none"
                )
                line = f"{ch.change_kind.value} {ch.json_path} ({span_txt})"
                if ch.before_snippet is not None or ch.after_snippet is not None:
                    line += f"\n  - {ch.before_snippet!r} -> {ch.after_snippet!r}"
                chg_lines.append(line)
            changes_txt = "\n".join(chg_lines)
        else:
            changes_txt = "-"

        cause_lines = [f"[{c.grade.value}] {c.code}" for c in d.candidate_causes]
        for item in d.unresolved:
            cause_lines.append(f"[unresolved] {item}")
        causes_txt = "\n".join(cause_lines) if cause_lines else "-"

        table.add_row(
            d.record_id,
            f"{d.identity.dialect}\n{d.identity.model} ({d.capability.support_level})",
            lineage_txt,
            usage_txt,
            changes_txt,
            causes_txt,
        )

    console.print(table)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cacherift",
        description="CacheRift: Prompt Cache Reliability for AI Agents.",
    )
    parser.add_argument("--version", action="store_true", help="Show version and exit.")
    sub = parser.add_subparsers(dest="command")

    analyze_p = sub.add_parser(
        "analyze", help="Diagnose prompt cache behavior from a JSONL capture file."
    )
    analyze_p.add_argument("capture_path", type=Path, help="Path to JSONL capture file.")
    analyze_p.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format (default: table).",
    )
    analyze_p.add_argument(
        "--pricing-table",
        default=None,
        help="Optional versioned pricing table ID (e.g. default-2026-11).",
    )
    analyze_p.add_argument(
        "--include-snippets",
        action="store_true",
        help="Include truncated prompt/unit snippets in output (disabled by default).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.version:
        sys.stdout.write(f"cacherift {__version__}\n")
        return 0

    if args.command != "analyze":
        parser.print_help()
        return 2

    capture_path: Path = args.capture_path
    if not capture_path.is_file():
        sys.stderr.write(f"Capture file not found: {capture_path}\n")
        return 1

    price_reg = PricingRegistry.load_default()
    if args.pricing_table is not None and price_reg.get_table(args.pricing_table) is None:
        sys.stderr.write(f"Unknown pricing table id: {args.pricing_table}\n")
        return 1

    records = load_capture_records(capture_path)
    diagnoses = analyze_records(
        records,
        pricing_registry=price_reg,
        pricing_table_id=args.pricing_table,
        include_snippets=bool(args.include_snippets),
    )

    if args.format == "json":
        payload = {
            "cacherift_version": __version__,
            "include_snippets": bool(args.include_snippets),
            "turns": [_to_json_safe(d) for d in diagnoses],
        }
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 0

    console = Console(file=sys.stdout, width=140, markup=False)
    _render_table(diagnoses, console)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
