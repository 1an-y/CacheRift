"""Independent versioned pricing estimation engine decoupled from usage parsing."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from cacherift.core.models import (
    CostEstimate,
    EndpointIdentity,
    UsageObservation,
    UsageState,
)


@dataclass(frozen=True, slots=True)
class PricingRule:
    provider: str
    model_pattern: str
    uncached_input_per_1m: float
    cache_read_per_1m: float
    output_per_1m: float
    write_per_1m: MappingProxyType[str, float]

    def match_score(self, provider: str, model: str) -> tuple[int, int] | None:
        if self.provider != provider:
            return None
        if self.model_pattern.endswith("*"):
            prefix = self.model_pattern[:-1]
            if not model.startswith(prefix):
                return None
            return (0, len(prefix))
        if model != self.model_pattern:
            return None
        return (1, len(self.model_pattern))

    def write_rate_for_tier(self, tier: str) -> float | None:
        if tier in self.write_per_1m:
            return self.write_per_1m[tier]
        if "default" in self.write_per_1m:
            return self.write_per_1m["default"]
        return None


@dataclass(frozen=True, slots=True)
class PricingTable:
    pricing_table_id: str
    rules: tuple[PricingRule, ...]
    source_version: str | None = None

    def lookup(self, provider: str, model: str) -> PricingRule | None:
        best_score: tuple[int, int] | None = None
        best_rule: PricingRule | None = None
        for rule in self._rules_iter():
            score = rule.match_score(provider, model)
            if score is not None and (best_score is None or score > best_score):
                best_score = score
                best_rule = rule
        return best_rule

    def _rules_iter(self) -> tuple[PricingRule, ...]:
        return self.rules


def estimate_cost(
    identity: EndpointIdentity,
    usage: UsageObservation,
    table: PricingTable | None,
) -> CostEstimate | None:
    """Estimate input/output USD cost against an explicit versioned PricingTable.

    Returns None whenever no pricing_table_id is declared on ``identity``, ``table`` does not
    match, usage is incomplete/missing/invalid, or the model/write-tier is unregistered.
    """
    if table is None or identity.pricing_table_id is None:
        return None
    if identity.pricing_table_id != table.pricing_table_id:
        return None
    if usage.state is not UsageState.COMPLETE or usage.buckets is None:
        return None

    rule = table.lookup(identity.provider, identity.model)
    if rule is None:
        return None

    buckets = usage.buckets
    write_cost = 0.0
    for wb in buckets.cache_writes:
        rate = rule.write_rate_for_tier(wb.tier)
        if rate is None:
            return None
        write_cost += (wb.tokens * rate) / 1_000_000.0

    input_cost = (
        buckets.uncached_input * rule.uncached_input_per_1m
        + buckets.cache_read * rule.cache_read_per_1m
    ) / 1_000_000.0 + write_cost
    output_cost = (buckets.output * rule.output_per_1m) / 1_000_000.0
    total_cost = input_cost + output_cost
    uncached_baseline = (buckets.total_input * rule.uncached_input_per_1m) / 1_000_000.0
    delta_vs_uncached = uncached_baseline - input_cost

    return CostEstimate(
        pricing_table_id=table.pricing_table_id,
        estimated_input_cost_usd=input_cost,
        estimated_output_cost_usd=output_cost,
        estimated_total_cost_usd=total_cost,
        uncached_baseline_input_cost_usd=uncached_baseline,
        estimated_input_delta_vs_uncached_usd=delta_vs_uncached,
    )
