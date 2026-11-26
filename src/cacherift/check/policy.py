"""Check policy: which diagnosed conditions block a build, warn, or are deliberately ignored."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cacherift.core.models import ChangeKind

Severity = Literal["error", "warning", "ignore"]


class PolicyError(ValueError):
    """The policy, or the file holding it, cannot be used as given."""


CHANGE_RULES: frozenset[str] = frozenset(kind.value for kind in ChangeKind)
# Cause codes the diagnoser emits that are not a restatement of an observed change.
CAUSE_RULES: frozenset[str] = frozenset(
    {
        "warm_cache_without_local_baseline",
        "ambiguous_lineage",
        "partial_prefix_cache_hit",
        "unexplained_low_hit",
        "response_cache_key_diverged",
        "possible_below_min_cache_tokens",
        "possible_provider_routing_or_eviction",
        "possible_gateway_param_forwarding_issue",
        "possible_ttl_expiry",
        "non_cacheable_top_level_instructions",
        "provider_reported_diagnostics",
    }
)
SPECIAL_RULES: frozenset[str] = frozenset(
    {"usage_invalid", "usage_missing", "usage_incomplete", "no_baseline"}
)
# Cause codes that restate one observed change; accepted in config as that change's alias.
ECHO_CAUSE_TO_CHANGE: Mapping[str, str] = {
    "existing_prefix_mutated": "mutated_existing",
    "existing_prefix_deleted": "deleted_existing",
    "existing_prefix_reordered": "reordered_existing",
    "history_compacted_or_truncated": "compacted_or_truncated",
    "upstream_layer_inserted": "inserted_before_tail",
    "cache_config_changed": "config_changed",
    "wire_formatting_only": "wire_only_diff",
}
# Canonical rule ids. Config files may also name an echo alias; it counts as its change kind.
ALL_RULES: frozenset[str] = CHANGE_RULES | CAUSE_RULES | SPECIAL_RULES

_DEFAULT_ERROR: frozenset[str] = frozenset(
    {
        "mutated_existing",
        "reordered_existing",
        "deleted_existing",
        "inserted_before_tail",
        "non_cacheable_top_level_instructions",
        "usage_invalid",
    }
)
_DEFAULT_WARNING: frozenset[str] = frozenset(
    {
        "compacted_or_truncated",
        "config_changed",
        "wire_only_diff",
        "ambiguous_lineage",
        "unexplained_low_hit",
        "response_cache_key_diverged",
    }
)
_RULE_SETS = ("error", "warning", "ignore")
_SETTINGS = (*_RULE_SETS, "ignore_paths")


def normalize_rule(rule_id: str) -> str:
    """Map an echo cause code onto the change kind it restates; other IDs are returned as is."""
    return ECHO_CAUSE_TO_CHANGE.get(rule_id, rule_id)


@dataclass(frozen=True, slots=True)
class CheckPolicy:
    """Resolved severities: every rule in exactly one set, everything unlisted is allowed."""

    error: frozenset[str]
    warning: frozenset[str]
    ignore: frozenset[str] = frozenset()
    ignore_paths: tuple[str, ...] = ()

    @classmethod
    def default(cls) -> CheckPolicy:
        return cls(error=_DEFAULT_ERROR, warning=_DEFAULT_WARNING, ignore=frozenset())

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> CheckPolicy:
        """Apply a config table on top of the defaults.

        A rule the table names gets the named severity (ignore > error > warning when it is
        named more than once); rules it does not name keep their default.
        """
        unknown_settings = sorted(set(mapping) - set(_SETTINGS))
        if unknown_settings:
            raise PolicyError(
                f"unknown check settings: {', '.join(unknown_settings)}; "
                f"valid settings: {', '.join(_SETTINGS)}"
            )
        named = {setting: _named_rules(mapping.get(setting, ()), setting) for setting in _RULE_SETS}

        error: set[str] = set()
        warning: set[str] = set()
        ignore: set[str] = set()
        for rule in ALL_RULES | named["error"] | named["warning"] | named["ignore"]:
            if rule in named["ignore"]:
                ignore.add(rule)
            elif rule in named["error"]:
                error.add(rule)
            elif rule in named["warning"]:
                warning.add(rule)
            elif rule in _DEFAULT_ERROR:
                error.add(rule)
            elif rule in _DEFAULT_WARNING:
                warning.add(rule)
        return cls(
            frozenset(error),
            frozenset(warning),
            frozenset(ignore),
            _path_patterns(mapping.get("ignore_paths", ())),
        )

    def severity(self, rule_id: str) -> Severity | None:
        """Severity of a rule ID (echo aliases included), or None when it is allowed."""
        rule = normalize_rule(rule_id)
        if rule in self.ignore:
            return "ignore"
        if rule in self.error:
            return "error"
        if rule in self.warning:
            return "warning"
        return None


def _named_rules(value: Any, setting: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple)):
        raise PolicyError(f"check setting '{setting}' must be a list of rule ids")
    rules = set()
    for item in value:
        if not isinstance(item, str):
            raise PolicyError(f"check setting '{setting}' must be a list of rule ids")
        if normalize_rule(item) not in ALL_RULES:
            raise PolicyError(
                f"unknown rule id '{item}' in check setting '{setting}'; valid ids: "
                + ", ".join(sorted(CHANGE_RULES | CAUSE_RULES | SPECIAL_RULES))
            )
        rules.add(normalize_rule(item))
    return frozenset(rules)


def _path_patterns(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise PolicyError("check setting 'ignore_paths' must be a list of glob patterns")
    return tuple(value)


def load_policy(config_path: Path | None = None, *, start: Path | None = None) -> CheckPolicy:
    """Load the policy from an explicit file, else from the project's own config, else defaults.

    Without ``config_path`` the first of ``cacherift.toml`` / ``pyproject.toml`` in ``start``
    that holds a check table wins. With ``config_path`` the file must exist and hold one.
    """
    if config_path is not None:
        table = _check_table(_read_toml(config_path))
        if table is None:
            raise PolicyError(f"no check settings found in {config_path}")
        return CheckPolicy.from_mapping(table)
    for name in ("cacherift.toml", "pyproject.toml"):
        candidate = (start or Path.cwd()) / name
        if candidate.is_file():
            table = _check_table(_read_toml(candidate))
            if table is not None:
                return CheckPolicy.from_mapping(table)
    return CheckPolicy.default()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"cannot read config file {path}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"invalid TOML in {path}: {exc}") from exc


def _check_table(document: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The check table of a config file: `[tool.cacherift.check]` or a top-level `[check]`."""
    tool = document.get("tool")
    if isinstance(tool, Mapping):
        cacherift = tool.get("cacherift")
        if isinstance(cacherift, Mapping):
            nested = cacherift.get("check")
            if isinstance(nested, Mapping):
                return nested
    table = document.get("check")
    return table if isinstance(table, Mapping) else None
