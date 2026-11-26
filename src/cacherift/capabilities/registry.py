"""Lookup registry for provider/model prompt-caching capabilities."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal, cast

from cacherift.core.freeze import freeze_json
from cacherift.core.models import CapabilityInfo
from cacherift.core.pricing import PricingRule, PricingTable

_UNKNOWN_CAPABILITY = CapabilityInfo(
    min_cache_tokens=None,
    ttl_semantics=None,
    ttl_seconds_hint=None,
    prefix_order_note=None,
    support_level="unknown",
    source_version=None,
    unit_order=(),
)


@dataclass(frozen=True, slots=True)
class _CapabilityRule:
    dialect: str
    provider: str
    model_pattern: str
    gateway_profile: str
    info: CapabilityInfo

    def match_score(
        self,
        dialect: str,
        provider: str,
        model: str,
        gateway_profile: str | None,
    ) -> tuple[int, int, int] | None:
        if self.dialect != dialect or self.provider != provider:
            return None

        if self.gateway_profile != "*":
            if gateway_profile != self.gateway_profile:
                return None
            gw_score = 1
        else:
            gw_score = 0

        if self.model_pattern.endswith("*"):
            prefix = self.model_pattern[:-1]
            if not model.startswith(prefix):
                return None
            return (0, len(prefix), gw_score)

        if model != self.model_pattern:
            return None
        return (1, len(self.model_pattern), gw_score)


class CapabilityRegistry:
    """Versioned static registry mapping (dialect, provider, model, gateway_profile) to info."""

    def __init__(self, rules: tuple[_CapabilityRule, ...]) -> None:
        self._rules = rules

    @classmethod
    def load_default(cls) -> CapabilityRegistry:
        raw_bytes = (
            resources.files("cacherift.capabilities.tables")
            .joinpath("default_capabilities.toml")
            .read_bytes()
        )
        return cls.from_toml_bytes(raw_bytes)

    @classmethod
    def from_toml_bytes(cls, data: bytes) -> CapabilityRegistry:
        parsed: dict[str, Any] = tomllib.loads(data.decode("utf-8"))
        rules: list[_CapabilityRule] = []
        for entry in parsed.get("capabilities", []):
            support_raw = str(entry.get("support_level", "unknown"))
            support_level: Literal["full", "usage_only", "unknown"] = cast(
                Literal["full", "usage_only", "unknown"],
                support_raw if support_raw in ("full", "usage_only", "unknown") else "unknown",
            )
            min_tokens = entry.get("min_cache_tokens")
            ttl_hint = entry.get("ttl_seconds_hint")
            info = CapabilityInfo(
                min_cache_tokens=int(min_tokens) if min_tokens is not None else None,
                ttl_semantics=entry.get("ttl_semantics"),
                ttl_seconds_hint=int(ttl_hint) if ttl_hint is not None else None,
                prefix_order_note=entry.get("prefix_order_note"),
                support_level=support_level,
                source_version=entry.get("source_version"),
                unit_order=tuple(str(u) for u in entry.get("unit_order", ())),
            )
            rules.append(
                _CapabilityRule(
                    dialect=str(entry["dialect"]),
                    provider=str(entry["provider"]),
                    model_pattern=str(entry["model_pattern"]),
                    gateway_profile=str(entry.get("gateway_profile", "*")),
                    info=info,
                )
            )
        return cls(tuple(rules))

    def lookup(
        self,
        dialect: str,
        provider: str,
        model: str,
        gateway_profile: str | None = None,
    ) -> CapabilityInfo:
        """Return matching capability declaration or explicit unknown/None metadata."""
        best_score: tuple[int, int, int] | None = None
        best_info: CapabilityInfo = _UNKNOWN_CAPABILITY
        for rule in self._rules:
            score = rule.match_score(dialect, provider, model, gateway_profile)
            if score is not None and (best_score is None or score > best_score):
                best_score = score
                best_info = rule.info
        return best_info


class PricingRegistry:
    """Versioned static registry loading PricingTable entries from TOML."""

    def __init__(self, tables: dict[str, PricingTable]) -> None:
        self._tables = tables

    @classmethod
    def load_default(cls) -> PricingRegistry:
        raw_bytes = (
            resources.files("cacherift.capabilities.tables")
            .joinpath("default_pricing.toml")
            .read_bytes()
        )
        return cls.from_toml_bytes(raw_bytes)

    @classmethod
    def from_toml_bytes(cls, data: bytes) -> PricingRegistry:
        parsed: dict[str, Any] = tomllib.loads(data.decode("utf-8"))
        tables: dict[str, PricingTable] = {}
        for t_entry in parsed.get("tables", []):
            table_id = str(t_entry["pricing_table_id"])
            source_version = t_entry.get("source_version")
            rules: list[PricingRule] = []
            for r_entry in t_entry.get("rules", []):
                write_raw = {
                    str(k): float(v) for k, v in dict(r_entry.get("write_per_1m") or {}).items()
                }
                rules.append(
                    PricingRule(
                        provider=str(r_entry["provider"]),
                        model_pattern=str(r_entry["model_pattern"]),
                        uncached_input_per_1m=float(r_entry["uncached_input_per_1m"]),
                        cache_read_per_1m=float(r_entry["cache_read_per_1m"]),
                        output_per_1m=float(r_entry["output_per_1m"]),
                        write_per_1m=freeze_json(write_raw),
                    )
                )
            tables[table_id] = PricingTable(
                pricing_table_id=table_id,
                rules=tuple(rules),
                source_version=str(source_version) if source_version is not None else None,
            )
        return cls(tables)

    def get_table(self, pricing_table_id: str) -> PricingTable | None:
        return self._tables.get(pricing_table_id)

    def default_table_id(self) -> str | None:
        """ID of the newest registered ``default-*`` table, or None when there is none."""
        defaults = sorted(table_id for table_id in self._tables if table_id.startswith("default-"))
        return defaults[-1] if defaults else None
