"""`cacherift probe`: controlled active probing, dry-run unless ``--execute`` is passed."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from rich.console import Console
from rich.table import Table

from cacherift import __version__
from cacherift.capabilities.registry import PricingRegistry
from cacherift.cli import (
    EXIT_CONFIG,
    EXIT_FAILURE,
    EXIT_OK,
    analyze_records,
    emit_json,
    render_diagnosis_table,
    require_modules,
    to_json_safe,
)
from cacherift.core.models import EndpointIdentity, TurnDiagnosis
from cacherift.io.capture import dump_capture_record
from cacherift.io.redact import split_clean_url
from cacherift.probe.budget import ProbeBudget
from cacherift.probe.scenarios import SCENARIO_ORDER

if TYPE_CHECKING:
    import httpx

    from cacherift.core.models import CaptureRecord
    from cacherift.probe.runner import (
        PlannedScenario,
        PlannedStep,
        ProbePlan,
        ProbeRunner,
        ProbeRunResult,
    )

_DIALECT_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai_responses": ("openai", "OPENAI_API_KEY"),
    "anthropic_messages": ("anthropic", "ANTHROPIC_API_KEY"),
}
_ENDPOINT_SUFFIX: dict[str, str] = {
    "openai_responses": "/responses",
    "anthropic_messages": "/messages",
}


class _ConfigError(ValueError):
    """Invalid command-line configuration; reported as a single line with exit code 2."""


def register_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "probe",
        help="Send controlled micro-probes to an upstream and report its real cache behavior.",
        description=(
            "Controlled active probing. Without --execute nothing is sent: the plan and its "
            "worst-case cost are printed. Pass --execute to send; the raw request bytes are "
            "captured to JSONL and diagnosed like `cacherift analyze`."
        ),
    )
    parser.add_argument(
        "--dialect",
        required=True,
        choices=tuple(_DIALECT_DEFAULTS),
        help="Protocol dialect of the upstream.",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="API base URL without credentials, query or fragment (e.g. https://api.openai.com/v1).",
    )
    parser.add_argument("--model", required=True, help="Model name sent in every request.")
    parser.add_argument(
        "--provider",
        default=None,
        help="Provider identity used for capability and pricing lookup (default: by dialect).",
    )
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="Environment variable holding the API key "
        "(default: OPENAI_API_KEY or ANTHROPIC_API_KEY).",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=(*SCENARIO_ORDER, "all"),
        help="Scenario to run; repeatable. Default: all scenarios that apply to the dialect.",
    )
    parser.add_argument(
        "--ttl",
        choices=("5m", "1h"),
        default="5m",
        help="Anthropic cache_control TTL to probe (default: 5m).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually send the requests. Without it the command is a dry-run.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=12,
        help="Hard cap on requests sent in this run (default: 12).",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=0.05,
        help="Hard cap on worst-case cost in USD (default: 0.05).",
    )
    parser.add_argument(
        "--pricing-table",
        default=None,
        help="Versioned pricing table ID used for cost bounds (default: newest default-* table).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSONL capture path for --execute (default: cacherift-probe-<run id>.jsonl).",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format (default: table).",
    )
    return parser


def _usd(value: float | None) -> str:
    return "unpriced" if value is None else f"${value:.6f}"


def _endpoint(base_url: str, dialect: str) -> tuple[str, str]:
    """Return (full endpoint URL, URL path), rejecting anything that could carry a secret."""
    split_clean_url(base_url, "--base-url")
    url = base_url.rstrip("/") + _ENDPOINT_SUFFIX[dialect]
    return url, urlsplit(url).path


def _build_runner(
    args: argparse.Namespace, budget: ProbeBudget, transport: httpx.BaseTransport | None
) -> tuple[ProbeRunner, list[str] | None, str]:
    provider_default, key_env_default = _DIALECT_DEFAULTS[args.dialect]
    pricing = PricingRegistry.load_default()
    table_id = args.pricing_table or pricing.default_table_id()
    if table_id is None or pricing.get_table(table_id) is None:
        raise _ConfigError(f"unknown pricing table id: {args.pricing_table}")
    if args.max_requests < 1:
        raise _ConfigError("--max-requests must be at least 1")
    if args.max_cost_usd <= 0:
        raise _ConfigError("--max-cost-usd must be greater than 0")
    if args.ttl != "5m" and args.dialect != "anthropic_messages":
        raise _ConfigError("--ttl only applies to --dialect anthropic_messages")

    url, endpoint_path = _endpoint(args.base_url, args.dialect)
    from cacherift.probe.runner import ProbeRunner

    runner = ProbeRunner(
        EndpointIdentity(
            dialect=args.dialect,
            provider=args.provider or provider_default,
            model=args.model,
            endpoint_path=endpoint_path,
            pricing_table_id=table_id,
        ),
        url=url,
        api_key_env=args.api_key_env or key_env_default,
        ttl=args.ttl,
        budget=budget,
        transport=transport,
    )
    scenarios = None if not args.scenario or "all" in args.scenario else list(args.scenario)
    return runner, scenarios, url


def _step_json(step: PlannedStep) -> dict[str, Any]:
    return {
        "record_id": step.record_id,
        "label": step.label,
        "intent": step.intent,
        "parent_record_id": step.parent_record_id,
        "bytes": len(step.body),
        "estimated_tokens": step.estimated_tokens,
        "worst_case_usd": step.worst_case_usd,
    }


def _plan_json(plan: ProbePlan, budget: ProbeBudget, target: dict[str, str]) -> dict[str, Any]:
    return {
        "cacherift_version": __version__,
        "mode": "dry-run",
        "run_id": plan.run_id,
        "target": target,
        "budget": {"max_requests": budget.max_requests, "max_cost_usd": budget.max_cost_usd},
        "scenarios": [
            {
                "name": scenario.name,
                "fits_budget": scenario.fits_budget,
                "worst_case_usd": scenario.worst_case_usd,
                "steps": [_step_json(step) for step in scenario.steps],
            }
            for scenario in plan.scenarios
        ],
        "totals": {
            "requests": len(plan.planned_steps),
            "requested": len(plan.steps),
            "worst_case_usd": plan.worst_case_usd,
            "unpriced_scenarios": list(plan.unpriced),
        },
    }


def _status(scenario: PlannedScenario) -> str:
    if scenario.fits_budget:
        return "planned"
    if scenario.worst_case_usd is None:
        return "unpriced: cost cannot be bounded, will not be sent"
    return "skipped: over budget"


def _render_plan(plan: ProbePlan, budget: ProbeBudget, target: str) -> None:
    console = Console(file=sys.stdout, width=140, markup=False)
    console.print("CacheRift probe: dry-run, no requests sent.")
    console.print(f"run id: {plan.run_id} (record id = <run id>:<scenario>:<label>)")
    console.print(f"target: {target}")
    console.print(f"budget: at most {budget.max_requests} requests and ${budget.max_cost_usd:.2f}")
    table = Table()
    table.add_column("Step", style="bold")
    table.add_column("Bytes", justify="right")
    table.add_column("Est. tokens", justify="right")
    table.add_column("Worst-case USD", justify="right")
    table.add_column("Intent")
    for scenario in plan.scenarios:
        for step in scenario.steps:
            table.add_row(
                f"{step.scenario}:{step.label}",
                str(len(step.body)),
                str(step.estimated_tokens),
                _usd(step.worst_case_usd),
                step.intent,
            )
        table.add_row(
            f"{scenario.name} [{_status(scenario)}]", "", "", _usd(scenario.worst_case_usd), ""
        )
    console.print(table)
    console.print(
        f"total: {len(plan.planned_steps)} of {len(plan.steps)} requests planned, "
        f"worst case {_usd(plan.worst_case_usd)} of ${budget.max_cost_usd:.2f}"
    )


def _render_result(
    plan: ProbePlan,
    result: ProbeRunResult,
    diagnoses: list[TurnDiagnosis],
    output_path: Path,
    budget: ProbeBudget,
) -> None:
    console = Console(file=sys.stdout, width=140, markup=False)
    console.print(f"captured {len(result.records)} request(s) to {output_path}")
    console.print(
        f"spent about ${result.spent_usd:.6f} of the ${budget.max_cost_usd:.2f} cap "
        f"({budget.max_requests} request cap)"
    )
    over_budget = [
        scenario.name
        for scenario in plan.scenarios
        if not scenario.fits_budget and scenario.worst_case_usd is not None
    ]
    if over_budget:
        console.print(
            "skipped because the remaining budget cannot hold the whole scenario: "
            + ", ".join(over_budget)
            + " (raise --max-cost-usd or --max-requests to include them)"
        )
    render_diagnosis_table(diagnoses, console)


def run_command(args: argparse.Namespace, *, transport: httpx.BaseTransport | None = None) -> int:
    require_modules("probe", "probe", "httpx")
    from cacherift.probe.runner import ProbeConfigError

    budget = ProbeBudget(
        max_requests=args.max_requests,
        max_cost_usd=args.max_cost_usd,
        execute=bool(args.execute),
    )
    try:
        runner, scenarios, url = _build_runner(args, budget, transport)
        plan = runner.plan(scenarios)
    except ValueError as exc:  # ProbeConfigError, _ConfigError and the URL hygiene checks
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONFIG

    target = f"{args.dialect} {args.model} -> {url}"
    if not args.execute:
        if plan.unpriced:
            identity = runner.identity
            sys.stderr.write(
                f"no pricing rule prices {identity.model!r} for provider "
                f"{identity.provider!r}: --execute will refuse, "
                "because the cost cannot be bounded\n"
            )
        if args.format == "json":
            emit_json(_plan_json(plan, budget, {"url": url, "model": args.model}))
        else:
            _render_plan(plan, budget, target)
        return EXIT_OK

    # Everything that can be refused is refused here, while nothing has been paid for yet.
    output_path = args.output or Path(f"cacherift-probe-{plan.run_id}.jsonl")
    try:
        runner.preflight(plan)
        # Exclusive create: an existing capture is never overwritten, and an unwritable
        # destination shows up now rather than after the requests have been paid for.
        output = output_path.open("x", encoding="utf-8")
    except ValueError as exc:  # ProbeConfigError, _ConfigError and the URL hygiene checks
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONFIG
    except OSError as exc:
        reason = "refusing to overwrite it" if output_path.exists() else (exc.strerror or exc)
        sys.stderr.write(f"cannot create capture {output_path}: {reason}\n")
        return EXIT_CONFIG

    def persist(record: CaptureRecord) -> None:
        # Written as each response arrives: an interrupted run keeps everything it paid for.
        output.write(dump_capture_record(record) + "\n")
        output.flush()

    try:
        result = runner.execute(plan, on_record=persist)
    except ProbeConfigError as exc:
        output.close()
        output_path.unlink()  # nothing was sent, so the empty file would only mislead
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONFIG
    except OSError as exc:
        output.close()
        sys.stderr.write(
            f"cannot write capture {output_path}: {exc.strerror or exc}; "
            "the run was stopped, earlier captures are on disk\n"
        )
        return EXIT_FAILURE
    finally:
        output.close()
    diagnoses = analyze_records(result.records)

    if args.format == "json":
        emit_json(
            {
                "cacherift_version": __version__,
                "mode": "execute",
                "run_id": plan.run_id,
                "output": str(output_path),
                "budget": {
                    "max_requests": budget.max_requests,
                    "max_cost_usd": budget.max_cost_usd,
                    "requests_sent": len(result.records),
                    "spent_usd": result.spent_usd,
                },
                "stopped_reason": result.stopped_reason,
                "skipped_scenarios": list(plan.skipped),
                "unpriced_scenarios": list(plan.unpriced),
                "turns": [to_json_safe(diag) for diag in diagnoses],
            }
        )
    else:
        _render_result(plan, result, diagnoses, output_path, budget)

    if result.stopped_reason is not None:
        sys.stderr.write(f"probe stopped early: {result.stopped_reason}\n")
        return EXIT_FAILURE
    return EXIT_FAILURE if plan.skipped else EXIT_OK
