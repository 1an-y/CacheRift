"""`cacherift check`: fail a build when a capture shows a broken prompt cache prefix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from cacherift import __version__
from cacherift.check.evaluator import CheckReport, Finding, evaluate_diagnoses
from cacherift.check.policy import load_policy
from cacherift.cli import EXIT_CONFIG, analyze_records, emit_json
from cacherift.io.capture import load_capture_records


class _ConfigError(ValueError):
    """Invalid invocation; reported as a single line with exit code 2."""


def register_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "check",
        help="Exit non-zero when a JSONL capture shows a broken prompt cache prefix.",
        description=(
            "Evaluate a capture against structural prefix rules. Exit 0: no violation; 1: at "
            "least one error-level rule hit; 2: the capture or the policy could not be read. "
            "Never uses a hit-rate threshold."
        ),
    )
    parser.add_argument("capture_path", type=Path, help="Path to JSONL capture file.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML file holding the check settings (a [check] or [tool.cacherift.check] table). "
        "Default: cacherift.toml, then pyproject.toml, in the current directory.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json", "github"),
        default="table",
        help="Output format (default: table). `github` prints workflow commands only.",
    )
    return parser


def _escape_property(value: str) -> str:
    # GitHub splits properties on ':' and ','; '%' is its escape character.
    return value.replace("%", "%25").replace(":", "%3A").replace(",", "%2C")


def _escape_message(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _github_command(finding: Finding, capture: str) -> str:
    message = f"{finding.record_id}: {finding.rule} {finding.json_path or ''}".rstrip()
    return (
        f"::{finding.severity} file={_escape_property(capture)},line={finding.line}"
        f"::{_escape_message(message)}"
    )


def _render_table(report: CheckReport, capture: str) -> None:
    console = Console(file=sys.stdout, width=140, markup=False)
    table = Table(title=f"CacheRift Check: {capture}")
    table.add_column("Line", justify="right")
    table.add_column("Record", style="bold")
    table.add_column("Severity")
    table.add_column("Rule")
    table.add_column("JSON path")
    table.add_column("Message")
    for finding in report.findings:
        table.add_row(
            str(finding.line),
            finding.record_id,
            finding.severity,
            finding.rule,
            finding.json_path or "",
            finding.message,
        )
    console.print(table)
    summary = report.summary
    console.print(
        f"{summary['errors']} error(s), {summary['warnings']} warning(s), "
        f"{summary['ignored']} ignored"
    )


def run_command(args: argparse.Namespace) -> int:
    capture: Path = args.capture_path
    try:
        policy = load_policy(args.config)
        if not capture.is_file():
            raise _ConfigError(f"capture file not found: {capture}")
        try:
            records = load_capture_records(capture)
        except (ValueError, KeyError, TypeError) as exc:
            raise _ConfigError(f"cannot read capture {capture}: {exc!r}") from exc
    except ValueError as exc:  # policy problems, a missing or malformed capture
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONFIG

    report = evaluate_diagnoses(analyze_records(records), policy)
    if args.format == "json":
        emit_json(
            {
                "cacherift_version": __version__,
                "capture": str(capture),
                "summary": report.summary,
                "exit_code": report.exit_code,
                "findings": [finding.to_json() for finding in report.findings],
            }
        )
    elif args.format == "github":
        for finding in report.findings:
            if finding.severity != "ignore":
                sys.stdout.write(_github_command(finding, str(capture)) + "\n")
    else:
        _render_table(report, str(capture))
    return report.exit_code
