"""`cacherift repair`: rewrite whitelisted prefix defects in a capture, dry-run by default."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from cacherift import __version__
from cacherift.cli import EXIT_CONFIG, EXIT_OK, analyze_records, emit_json
from cacherift.io.capture import dump_capture_record, load_capture_records
from cacherift.repair.engine import repair_records
from cacherift.repair.models import RepairEdit, RepairResult
from cacherift.repair.rules import ALL_RULES


class _ConfigError(ValueError):
    """Invalid invocation; reported as a single line with exit code 2."""


def register_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "repair",
        help="Rewrite whitelisted prefix defects in a capture; audited, dry-run by default.",
        description=(
            "Applies only rules that are provably equivalent for the model (tool key order, "
            "tool array order, static top-level instructions, stale Anthropic breakpoints). "
            "Prints the audit; writes only what --apply --output or --audit-out names."
        ),
    )
    parser.add_argument("capture_path", type=Path, help="Path to JSONL capture file.")
    parser.add_argument(
        "--rule",
        action="append",
        default=None,
        metavar="NAME",
        help=f"Rule to enable; repeatable. Default: all. One of: {', '.join(ALL_RULES)}.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repaired capture. Requires --output. Without it nothing is written.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path for the repaired JSONL. Must not already exist.",
    )
    parser.add_argument(
        "--audit-out",
        type=Path,
        default=None,
        help="Write the audit JSON to this path (also in dry-run). Must not already exist.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format (default: table).",
    )
    return parser


def _rules(requested: list[str] | None) -> tuple[str, ...]:
    if not requested or "all" in requested:
        return ALL_RULES
    chosen = tuple(dict.fromkeys(requested))
    unknown = sorted(set(chosen) - set(ALL_RULES))
    if unknown:
        raise _ConfigError(
            f"unknown repair rule(s): {', '.join(unknown)}; valid rules: {', '.join(ALL_RULES)}"
        )
    return chosen


def _document(
    result: RepairResult, capture: Path, rules: tuple[str, ...], mode: str
) -> dict[str, Any]:
    return {
        "cacherift_version": __version__,
        "mode": mode,
        "capture": str(capture),
        "rules": list(rules),
        "summary": result.summary,
        "audit": [entry.to_json() for entry in result.audit],
    }


def _count(length: int | None) -> str:
    return "-" if length is None else str(length)


def _sizes(edit: RepairEdit) -> str:
    return f"{_count(edit.before_len)} -> {_count(edit.after_len)}"


def _render(result: RepairResult, capture: Path, mode: str) -> None:
    console = Console(file=sys.stdout, width=140, markup=False)
    console.print(f"CacheRift repair ({mode}): {capture}")
    if result.audit:
        table = Table()
        table.add_column("Record", style="bold")
        table.add_column("Line", justify="right")
        table.add_column("Result")
        table.add_column("Rule")
        table.add_column("JSON path")
        table.add_column("Bytes before -> after")
        table.add_column("Detail")
        for entry in result.audit:
            if not entry.applied:
                table.add_row(entry.record_id, str(entry.line), f"refused: {entry.blocked_reason}")
            for edit in entry.edits:
                table.add_row(
                    entry.record_id,
                    str(entry.line),
                    "repaired",
                    edit.rule,
                    edit.json_path,
                    _sizes(edit),
                    edit.detail,
                )
        console.print(table)
    summary = result.summary
    console.print(
        f"{summary['records']} record(s): {summary['repaired']} repaired, "
        f"{summary['blocked']} refused, "
        f"{summary['records'] - summary['repaired'] - summary['blocked']} untouched."
    )


def run_command(args: argparse.Namespace) -> int:
    capture: Path = args.capture_path
    try:
        rules = _rules(args.rule)
        if args.apply != (args.output is not None):
            raise _ConfigError("--apply and --output must be given together")
        targets = [path for path in (args.output, args.audit_out) if path is not None]
        existing = [str(path) for path in targets if path.exists()]
        if existing:
            # A repaired capture is not a real capture: never let one replace the real thing.
            raise _ConfigError(f"refusing to overwrite existing file(s): {', '.join(existing)}")
        if not capture.is_file():
            raise _ConfigError(f"capture file not found: {capture}")
        try:
            records = load_capture_records(capture)
        except (ValueError, KeyError, TypeError) as exc:
            raise _ConfigError(f"cannot read capture {capture}: {exc!r}") from exc
    except ValueError as exc:  # _ConfigError and the capture loader
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONFIG

    result = repair_records(records, analyze_records(records), rules=rules)
    mode = "apply" if args.apply else "dry-run"
    document = _document(result, capture, rules, mode)
    if args.audit_out is not None:
        args.audit_out.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if args.output is not None:
        args.output.write_text(
            "".join(dump_capture_record(record) + "\n" for record in result.records),
            encoding="utf-8",
        )

    if args.format == "json":
        emit_json(document)
    else:
        _render(result, capture, mode)
    return EXIT_OK
