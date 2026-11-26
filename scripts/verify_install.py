"""Checks that run against an *installed* cacherift. CI runs them, and so can you.

    python scripts/verify_install.py plugin-entrypoint
    python scripts/verify_install.py core-only
    python scripts/verify_install.py distribution dist

Always run them with the interpreter of the environment you want to check. The script needs
nothing beyond the standard library, so it also runs where only the core package is installed.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS = REPO_ROOT / "tests"
SUBCOMMANDS = ("analyze", "probe", "proxy", "check", "repair")
# The two extras the network-facing commands need; the core install has neither.
OPTIONAL_DEPENDENCIES = ("httpx", "uvicorn")
# Test files that import those dependencies. Every other test file must pass without them.
TESTS_NEEDING_OPTIONAL_DEPENDENCIES = ("test_probe.py", "test_proxy.py")
CAPTURED_FIXTURES = (
    TESTS / "fixtures" / "captured" / "openai_responses_turns.jsonl",
    TESTS / "fixtures" / "captured" / "anthropic_messages_turns.jsonl",
)
# The registries read these at run time, so a distribution that lacks them is broken.
REQUIRED_PACKAGE_DATA = (
    "cacherift/capabilities/tables/default_capabilities.toml",
    "cacherift/capabilities/tables/default_pricing.toml",
)

# A test module for a directory that has no conftest.py: the fixture can only come from the
# pytest11 entry point of the installed package.
ENTRYPOINT_TEST = '''\
import pytest


def turn(number: int) -> dict:
    history = []
    for earlier in range(1, number):
        history += [
            {"role": "user", "content": f"q{earlier}"},
            {"role": "assistant", "content": "a"},
        ]
    history.append({"role": "user", "content": f"q{number}"})
    tool = {"type": "function", "name": "search", "parameters": {"type": "object"}}
    return {"model": "gpt-4o", "tools": [tool], "input": history}


def test_a_growing_conversation_keeps_its_prefix(cacherift_session):
    for number in (1, 2, 3):
        cacherift_session.add(turn(number))
    assert cacherift_session.assert_stable().errors == ()


def test_a_reordered_tool_is_caught(cacherift_session):
    cacherift_session.add(turn(1))
    jittered = turn(2)
    jittered["tools"][0] = {"parameters": {"type": "object"}, "name": "search", "type": "function"}
    cacherift_session.add(jittered)
    with pytest.raises(AssertionError, match="reordered_existing"):
        cacherift_session.assert_stable()
'''


class Report:
    """Collects the outcome of every check and prints one line per check as it runs."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        """Record one check; ``detail`` is appended to the line when the check fails."""
        if ok:
            self.passed += 1
            print(f"  ok    {what}")
        else:
            message = what if not detail else f"{what}: {detail}"
            self.failures.append(message)
            print(f"  FAIL  {message}")
        return ok

    def finish(self, title: str) -> int:
        total = self.passed + len(self.failures)
        if self.failures:
            print(f"{title}: {len(self.failures)} of {total} checks FAILED")
            return 1
        print(f"{title}: {total} checks passed")
        return 0


def _tail(result: subprocess.CompletedProcess[str], limit: int = 400) -> str:
    """The end of a command's combined output, for a failure message."""
    return (result.stdout + result.stderr).strip()[-limit:]


def _ended_cleanly(result: subprocess.CompletedProcess[str], *exit_codes: int) -> bool:
    """True when the command ended with one of ``exit_codes`` and printed no traceback."""
    return result.returncode in exit_codes and "Traceback" not in result.stdout + result.stderr


def _child_env() -> dict[str, str]:
    """The current environment minus anything that could point at another copy of the package."""
    return {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}


def _cacherift_command() -> list[str]:
    """The installed `cacherift` console script, falling back to the module entry point."""
    for name in ("cacherift", "cacherift.exe"):
        candidate = Path(sys.executable).parent / name
        if candidate.is_file():
            return [str(candidate)]
    return [sys.executable, "-m", "cacherift.cli"]


def _run(command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command in the environment under check and capture what it prints."""
    return subprocess.run(
        list(command), capture_output=True, text=True, env=_child_env(), cwd=cwd, check=False
    )


def _pytest(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run pytest in the environment under check, without writing a cache."""
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *arguments]
    return _run(command, cwd=cwd)


def core_only() -> int:
    """The core install works, and works without the optional dependencies."""
    report = Report()
    cacherift = _cacherift_command()
    print(f"core-only isolation, interpreter {sys.executable}")

    for name in OPTIONAL_DEPENDENCIES:
        if not report.check(
            importlib.util.find_spec(name) is None,
            f"{name} is not installed",
            "this check needs an environment that holds only the core package and pytest",
        ):
            return report.finish("core-only isolation")

    try:
        package = importlib.import_module("cacherift")
    except ImportError as exc:
        report.check(False, "the cacherift package imports", str(exc))
        return report.finish("core-only isolation")
    location = Path(package.__file__ or ".").parent
    print(f"  cacherift {package.__version__} from {location}")

    def run(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return _run([*cacherift, *arguments], cwd=cwd)

    result = run("--version")
    report.check(
        result.returncode == 0 and result.stdout.strip() == f"cacherift {package.__version__}",
        "`cacherift --version` prints the package version",
        _tail(result),
    )

    result = run("--help")
    report.check(
        result.returncode == 0 and all(name in result.stdout for name in SUBCOMMANDS),
        "`cacherift --help` lists every subcommand",
        _tail(result),
    )
    for name in SUBCOMMANDS:
        result = run(name, "--help")
        report.check(
            _ended_cleanly(result, 0),
            f"`cacherift {name} --help` works without the optional dependencies",
            _tail(result),
        )

    # A command that needs an extra must say so in one line on stderr, print nothing on stdout,
    # and never show a traceback.
    needs_an_extra = (
        (
            "probe",
            [
                "--dialect",
                "openai_responses",
                "--base-url",
                "https://example.invalid/v1",
                "--model",
                "gpt-6.1",
            ],
        ),
        ("proxy", ["--upstream", "https://example.invalid", "--output", "unused.jsonl"]),
    )
    for name, arguments in needs_an_extra:
        result = run(name, *arguments)
        lines = result.stderr.strip().splitlines()
        report.check(
            result.returncode == 2
            and result.stdout == ""
            and len(lines) == 1
            and f"cacherift[{name}]" in lines[0]
            and "Traceback" not in result.stderr,
            f"`cacherift {name}` without its extra exits 2 with a one-line install hint",
            _tail(result),
        )

    # The offline commands must run end to end on real captures. `repair` is started from an
    # empty directory: a dry run is the default, so nothing may appear there.
    with tempfile.TemporaryDirectory() as scratch:
        for fixture in CAPTURED_FIXTURES:
            result = run("analyze", str(fixture))
            report.check(
                _ended_cleanly(result, 0),
                f"`cacherift analyze` runs on {fixture.name}",
                _tail(result),
            )
            result = run("check", str(fixture))
            report.check(
                _ended_cleanly(result, 0, 1),
                f"`cacherift check` runs on {fixture.name} (exit code {result.returncode})",
                _tail(result),
            )
            result = run("repair", str(fixture), cwd=Path(scratch))
            report.check(
                _ended_cleanly(result, 0) and not list(Path(scratch).iterdir()),
                f"`cacherift repair` in dry-run mode runs on {fixture.name} and writes nothing",
                _tail(result),
            )

    # Every test file that needs no extra. `-o pythonpath=` clears the setting that would make
    # the tests import the source tree, so they exercise the installed package instead.
    result = _pytest(
        str(TESTS),
        "-o",
        "pythonpath=",
        *(f"--ignore={TESTS / name}" for name in TESTS_NEEDING_OPTIONAL_DEPENDENCIES),
        cwd=REPO_ROOT,
    )
    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "no output"
    report.check(
        result.returncode == 0,
        f"the tests run without the optional dependencies ({summary})",
        result.stdout[-1500:] + result.stderr[-1500:],
    )
    return report.finish("core-only isolation")


def plugin_entrypoint() -> int:
    """`cacherift_session` is provided by the entry point alone, where there is no conftest."""
    report = Report()
    print(f"pytest plugin entry point, interpreter {sys.executable}")

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "test_plugin_entrypoint.py").write_text(ENTRYPOINT_TEST, encoding="utf-8")

        result = _pytest(str(root), cwd=root)
        report.check(
            result.returncode == 0 and "2 passed" in result.stdout,
            "the cacherift_session fixture works in a directory that has no conftest.py",
            _tail(result, 1200),
        )
        # Negative control: with the plugin switched off the same tests cannot run, which
        # proves the pass above came from the entry point and not from somewhere else.
        result = _pytest(str(root), "-p", "no:cacherift", cwd=root)
        report.check(
            result.returncode != 0 and "cacherift_session" in result.stdout + result.stderr,
            "switching the plugin off breaks the same tests",
            _tail(result, 1200),
        )
    return report.finish("pytest plugin entry point")


def distribution(dist_dir: Path) -> int:
    """The built sdist and wheel carry the package data and the license."""
    report = Report()
    print(f"distribution contents, {dist_dir}")
    wheels = sorted(dist_dir.glob("cacherift-*.whl"))
    sdists = sorted(dist_dir.glob("cacherift-*.tar.gz"))
    built = report.check(
        len(wheels) == 1 and len(sdists) == 1,
        "exactly one wheel and one sdist were built",
        str(sorted(path.name for path in dist_dir.iterdir())),
    )
    if not built:
        return report.finish("distribution contents")

    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel_names = wheel.namelist()
    for name in REQUIRED_PACKAGE_DATA:
        report.check(name in wheel_names, f"the wheel carries {name}")
    # PEP 639 puts the license under `<name>-<version>.dist-info/licenses/`; older setuptools
    # builds kept it directly under `.dist-info/`.
    report.check(
        any(
            name.endswith((".dist-info/licenses/LICENSE", ".dist-info/LICENSE"))
            for name in wheel_names
        ),
        "the wheel carries LICENSE",
    )

    with tarfile.open(sdists[0]) as sdist:
        sdist_names = [member.name for member in sdist.getmembers()]
    top = sdists[0].name[: -len(".tar.gz")]
    for name in REQUIRED_PACKAGE_DATA:
        report.check(f"{top}/src/{name}" in sdist_names, f"the sdist carries src/{name}")
    report.check(f"{top}/LICENSE" in sdist_names, "the sdist carries LICENSE")
    return report.finish("distribution contents")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="check", required=True)
    sub.add_parser("plugin-entrypoint", help=plugin_entrypoint.__doc__)
    sub.add_parser("core-only", help=core_only.__doc__)
    distribution_parser = sub.add_parser("distribution", help=distribution.__doc__)
    distribution_parser.add_argument("dist_dir", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check == "core-only":
        return core_only()
    if args.check == "plugin-entrypoint":
        return plugin_entrypoint()
    return distribution(args.dist_dir)


if __name__ == "__main__":
    raise SystemExit(main())
