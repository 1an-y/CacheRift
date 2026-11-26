"""`cacherift proxy`: a local read-only capture proxy for real agent traffic."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cacherift.capabilities.registry import PricingRegistry
from cacherift.cli import EXIT_CONFIG, EXIT_OK, DiagnosisSession, require_modules
from cacherift.io.redact import split_clean_url

_DIALECTS = (
    "auto",
    "openai_responses",
    "anthropic_messages",
    "openai_chat",
    "deepseek_chat",
    "gemini_generate_content",
)


class _ConfigError(ValueError):
    """Invalid command-line configuration; reported as a single line with exit code 2."""


def register_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "proxy",
        help="Capture real agent traffic through a local read-only proxy.",
        description=(
            "Listen locally, forward every request to the upstream byte for byte, and append "
            "each model request (raw body, redacted headers, response or SSE events) to a "
            "JSONL file that `cacherift analyze`, `check` and `repair` read directly."
        ),
    )
    parser.add_argument(
        "--upstream",
        required=True,
        help="Upstream base URL without credentials, query or fragment (e.g. https://api.openai.com).",
    )
    parser.add_argument(
        "--listen",
        default="127.0.0.1:8787",
        help="host:port to listen on (default: 127.0.0.1:8787).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Append-only JSONL capture path.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Print a one-line summary to stderr after each captured request.",
    )
    parser.add_argument(
        "--dialect",
        choices=_DIALECTS,
        default="auto",
        help="Dialect recorded on captures; auto detects it from the request path.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Provider identity recorded on captures; default: derived from the upstream host.",
    )
    parser.add_argument(
        "--pricing-table",
        default=None,
        help="Optional versioned pricing table ID stamped on captures for cost estimates.",
    )
    return parser


def _listener(spec: str) -> tuple[str, int]:
    host, separator, port = spec.rpartition(":")
    if not separator or not host:
        raise _ConfigError("--listen must be host:port, e.g. 127.0.0.1:8787")
    try:
        number = int(port)
    except ValueError:
        raise _ConfigError(f"--listen port must be an integer, got {port!r}") from None
    if not 0 < number < 65536:
        raise _ConfigError("--listen port must be between 1 and 65535")
    return host.strip("[]"), number


def run_command(args: argparse.Namespace) -> int:
    require_modules("proxy", "proxy", "httpx", "uvicorn")
    import uvicorn

    from cacherift.proxy.app import ProxyApp
    from cacherift.proxy.recorder import CaptureRecorder

    try:
        split_clean_url(args.upstream, "--upstream")
        host, port = _listener(args.listen)
        if args.pricing_table is not None and (
            PricingRegistry.load_default().get_table(args.pricing_table) is None
        ):
            raise _ConfigError(f"unknown pricing table id: {args.pricing_table}")
    except ValueError as exc:  # covers _ConfigError and the URL hygiene checks
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONFIG

    recorder = CaptureRecorder(
        args.output, live=bool(args.live), session=DiagnosisSession() if args.live else None
    )
    app = ProxyApp(
        upstream=args.upstream,
        recorder=recorder,
        dialect=args.dialect,
        provider=args.provider,
        pricing_table_id=args.pricing_table,
    )
    try:
        sys.stderr.write(
            f"cacherift proxy: http://{host}:{port} -> {app.upstream} "
            f"(capturing to {args.output})\n"
        )
        # uvicorn shuts the app down through its lifespan hook on Ctrl-C / SIGTERM, and then
        # exits by re-raising the signal, so the line after run() is not always reached.
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        recorder.close()  # idempotent; covers hosts that skip the lifespan protocol
    return EXIT_OK
