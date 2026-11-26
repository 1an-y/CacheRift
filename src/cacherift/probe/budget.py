"""Pre-send budget enforcement for controlled probes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cacherift.core.freeze import freeze_json
from cacherift.core.models import (
    CacheWriteBucket,
    EndpointIdentity,
    TokenBuckets,
    UsageObservation,
    UsageState,
)
from cacherift.core.pricing import PricingTable, estimate_cost

# "Exactly at the cap" must not be rejected because of rounding in the last float bit.
_EPS_USD = 1e-9


class BudgetExceededError(RuntimeError):
    """Raised before a request is sent because it would cross the budget."""


@dataclass(frozen=True, slots=True)
class ProbeBudget:
    """Hard caps for one probe run; nothing is sent unless ``execute`` is set explicitly."""

    max_requests: int = 12
    max_cost_usd: float = 0.10
    execute: bool = False


def _worst_usage(
    tokens: int,
    output_tokens: int,
    writes: tuple[CacheWriteBucket, ...],
) -> UsageObservation:
    return UsageObservation(
        state=UsageState.COMPLETE,
        buckets=TokenBuckets(
            uncached_input=tokens - sum(w.tokens for w in writes),
            cache_read=0,
            cache_writes=writes,
            output=output_tokens,
            total_input=tokens,
            reported_hit_ratio=0.0,
        ),
        raw_fields=freeze_json({}),
    )


def worst_case_cost(
    identity: EndpointIdentity,
    body_bytes: bytes,
    table: PricingTable | None,
    *,
    max_output_tokens: int,
    write_tier: str | None = None,
) -> float | None:
    """Upper bound for one request, or None when the model has no pricing rule.

    Every input token is billed at the highest rate that can apply to it: the uncached rate,
    or the cache-write rate of the selected TTL tier when that is higher. Token buckets never
    overlap, so the maximum over these layouts bounds any real split. The token count is an
    estimate (heuristic_bytes_div_4); actual usage replaces it after each response.
    """
    tokens = len(body_bytes) // 4
    rule = table.lookup(identity.provider, identity.model) if table is not None else None
    if tokens <= 0 or rule is None:
        return None

    layouts: list[tuple[CacheWriteBucket, ...]] = [()]
    if write_tier is not None:
        layouts.append((CacheWriteBucket(write_tier, tokens),))
    estimates = [
        estimate_cost(identity, _worst_usage(tokens, max_output_tokens, layout), table)
        for layout in layouts
    ]
    costs = [e.estimated_total_cost_usd for e in estimates if e is not None]
    return max(costs) if costs else None


class BudgetTracker:
    """Running ledger that vets every request before it goes on the wire."""

    def __init__(self, budget: ProbeBudget) -> None:
        self._budget = budget
        self._sent = 0
        self._spent = 0.0

    @property
    def requests_sent(self) -> int:
        return self._sent

    @property
    def spent_usd(self) -> float:
        return self._spent

    @property
    def remaining_requests(self) -> int:
        return max(0, self._budget.max_requests - self._sent)

    @property
    def remaining_cost_usd(self) -> float:
        return max(0.0, self._budget.max_cost_usd - self._spent)

    def allows(self, requests: int, worst_case_usd: float) -> bool:
        return (
            requests <= self.remaining_requests
            and worst_case_usd <= self.remaining_cost_usd + _EPS_USD
        )

    def reserve(self, worst_case_usd: float) -> None:
        """Vet the next request; raise BudgetExceededError instead of letting it be sent."""
        if not self.allows(1, worst_case_usd):
            raise BudgetExceededError(
                f"budget exceeded: {self.remaining_requests} requests and "
                f"${self.remaining_cost_usd:.6f} left, next request needs at most "
                f"${worst_case_usd:.6f}"
            )

    def settle(self, actual_usd: float | None, worst_case_usd: float) -> None:
        """Book a sent request; an unknown actual cost keeps its worst-case envelope."""
        self._sent += 1
        self._spent += worst_case_usd if actual_usd is None else actual_usd


def select_scenarios(
    costs: Sequence[tuple[str, int, float | None]],
    budget: ProbeBudget,
) -> dict[str, bool]:
    """Admit scenarios as whole groups, in order, while their worst case fits the budget.

    An unpriced scenario (cost None) never fits: a request whose cost cannot be bounded is not sent.
    """
    used_requests = 0
    reserved_usd = 0.0
    fits: dict[str, bool] = {}
    for name, requests, worst_case_usd in costs:
        admitted = (
            worst_case_usd is not None
            and used_requests + requests <= budget.max_requests
            and reserved_usd + worst_case_usd <= budget.max_cost_usd + _EPS_USD
        )
        fits[name] = admitted
        if admitted and worst_case_usd is not None:
            used_requests += requests
            reserved_usd += worst_case_usd
    return fits
