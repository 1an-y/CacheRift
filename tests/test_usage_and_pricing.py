from __future__ import annotations

import pytest

from cacherift.capabilities.registry import PricingRegistry
from cacherift.core.freeze import freeze_json
from cacherift.core.models import (
    CacheWriteBucket,
    EndpointIdentity,
    TokenBuckets,
    UsageObservation,
    UsageState,
)
from cacherift.core.pricing import PricingRule, PricingTable, estimate_cost


def test_pricing_table_positive_savings_and_negative_write_premium() -> None:
    table = PricingTable(
        pricing_table_id="test-2026-11",
        rules=(
            PricingRule(
                provider="openai",
                model_pattern="gpt-6.1*",
                uncached_input_per_1m=2.50,
                cache_read_per_1m=0.25,
                output_per_1m=10.00,
                write_per_1m=freeze_json({}),
            ),
            PricingRule(
                provider="anthropic",
                model_pattern="claude-sonnet-4*",
                uncached_input_per_1m=3.00,
                cache_read_per_1m=0.30,
                output_per_1m=15.00,
                write_per_1m=freeze_json({"5m": 3.75, "1h": 6.00}),
            ),
        ),
    )

    # OpenAI cache read savings
    oai_id = EndpointIdentity(
        dialect="openai_responses",
        provider="openai",
        model="gpt-6.1",
        endpoint_path="/v1/responses",
        pricing_table_id="test-2026-11",
    )
    oai_usage = UsageObservation(
        state=UsageState.COMPLETE,
        buckets=TokenBuckets(
            uncached_input=140,
            cache_read=1480,
            cache_writes=(),
            output=95,
            total_input=1620,
            reported_hit_ratio=1480 / 1620,
        ),
        raw_fields=freeze_json({}),
    )
    est_oai = estimate_cost(oai_id, oai_usage, table)
    assert est_oai is not None
    assert est_oai.pricing_table_id == "test-2026-11"
    assert abs(est_oai.estimated_input_cost_usd - 0.000720) < 1e-12
    assert abs(est_oai.estimated_output_cost_usd - 0.000950) < 1e-12
    assert abs(est_oai.uncached_baseline_input_cost_usd - 0.004050) < 1e-12
    assert abs(est_oai.estimated_input_delta_vs_uncached_usd - 0.003330) < 1e-12

    # Anthropic initial 1h write premium -> negative delta vs uncached
    ant_id = EndpointIdentity(
        dialect="anthropic_messages",
        provider="anthropic",
        model="claude-sonnet-4-6",
        endpoint_path="/v1/messages",
        pricing_table_id="test-2026-11",
    )
    ant_usage = UsageObservation(
        state=UsageState.COMPLETE,
        buckets=TokenBuckets(
            uncached_input=100,
            cache_read=0,
            cache_writes=(CacheWriteBucket("1h", 2000),),
            output=50,
            total_input=2100,
            reported_hit_ratio=0.0,
        ),
        raw_fields=freeze_json({}),
    )
    est_ant = estimate_cost(ant_id, ant_usage, table)
    assert est_ant is not None
    assert abs(est_ant.estimated_input_cost_usd - 0.012300) < 1e-12
    assert abs(est_ant.uncached_baseline_input_cost_usd - 0.006300) < 1e-12
    assert abs(est_ant.estimated_input_delta_vs_uncached_usd - (-0.006000)) < 1e-12


def test_pricing_returns_none_without_table_id_or_complete_usage_or_known_model() -> None:
    registry = PricingRegistry.load_default()
    table = registry.get_table("default-2026-11")
    assert table is not None

    usage_ok = UsageObservation(
        state=UsageState.COMPLETE,
        buckets=TokenBuckets(100, 0, (), 10, 100, 0.0),
        raw_fields=freeze_json({}),
    )
    no_table_id = EndpointIdentity(
        "openai_responses", "openai", "gpt-6.1", "/v1/responses", pricing_table_id=None
    )
    assert estimate_cost(no_table_id, usage_ok, table) is None
    assert estimate_cost(no_table_id, usage_ok, None) is None

    unknown_model = EndpointIdentity(
        "openai_responses",
        "openai",
        "custom-unknown-model",
        "/v1/responses",
        pricing_table_id="default-2026-11",
    )
    assert estimate_cost(unknown_model, usage_ok, table) is None

    usage_missing = UsageObservation(
        state=UsageState.MISSING, buckets=None, raw_fields=freeze_json({})
    )
    known_id = EndpointIdentity(
        "openai_responses",
        "openai",
        "gpt-6.1-sol",
        "/v1/responses",
        pricing_table_id="default-2026-11",
    )
    assert estimate_cost(known_id, usage_missing, table) is None


def _default_rates(table: PricingTable, provider: str, model: str) -> tuple[float, float, float]:
    rule = table.lookup(provider, model)
    assert rule is not None, f"{provider}/{model} is not priced"
    return (rule.uncached_input_per_1m, rule.cache_read_per_1m, rule.output_per_1m)


def test_default_table_prices_the_usage_only_dialects_as_their_vendors_list_them() -> None:
    # Literals transcribed from the vendors' pricing pages; the sources and check dates sit in
    # comments next to each rule in default_pricing.toml.
    table = PricingRegistry.load_default().get_table("default-2026-11")
    assert table is not None

    deepseek_flash = (0.3, 0.006, 1.2)  # peak rates; off-peak is half
    assert _default_rates(table, "deepseek", "deepseek-flash") == deepseek_flash
    assert _default_rates(table, "deepseek", "deepseek-v4-flash") == deepseek_flash
    assert _default_rates(table, "deepseek", "deepseek-v4-flash-vision-exp") == deepseek_flash
    assert _default_rates(table, "deepseek", "deepseek-v4-pro") == (1.32, 0.044, 3.96)

    assert _default_rates(table, "gemini", "gemini-2.5-pro") == (1.25, 0.125, 10.00)
    assert _default_rates(table, "gemini", "gemini-2.5-flash") == (0.30, 0.03, 2.50)
    assert _default_rates(table, "gemini", "gemini-2.5-flash-lite") == (0.10, 0.01, 0.40)

    assert _default_rates(table, "openai", "gpt-4o") == (2.50, 1.25, 10.00)
    assert _default_rates(table, "openai", "gpt-4o-mini") == (0.15, 0.075, 0.60)
    assert _default_rates(table, "openai", "gpt-4.1") == (2.00, 0.50, 8.00)
    assert _default_rates(table, "openai", "gpt-4.1-mini") == (0.40, 0.10, 1.60)

    assert _default_rates(table, "openai", "gpt-6.1-sol") == (2.00, 0.10, 10.00)
    assert _default_rates(table, "openai", "gpt-6.2-sol") == (2.00, 0.10, 10.00)
    assert _default_rates(table, "openai", "gpt-6.2-astra") == (8.00, 0.40, 40.00)

    assert _default_rates(table, "anthropic", "claude-sonnet-4-6") == (3.00, 0.30, 15.00)
    assert _default_rates(table, "anthropic", "claude-opus-4-6") == (5.00, 0.50, 25.00)
    assert _default_rates(table, "anthropic", "claude-opus-4-1") == (15.00, 1.50, 75.00)
    assert _default_rates(table, "anthropic", "claude-haiku-4-5") == (1.00, 0.10, 5.00)
    opus_46 = table.lookup("anthropic", "claude-opus-4-6")
    haiku_45 = table.lookup("anthropic", "claude-haiku-4-5")
    assert opus_46 is not None and dict(opus_46.write_per_1m) == {
        "default": 6.25,
        "5m": 6.25,
        "1h": 10.00,
    }
    assert haiku_45 is not None and dict(haiku_45.write_per_1m) == {
        "default": 1.25,
        "5m": 1.25,
        "1h": 2.00,
    }


def test_default_table_leaves_untranscribed_neighbours_unpriced() -> None:
    table = PricingRegistry.load_default().get_table("default-2026-11")
    assert table is not None

    # Each has a price of its own on a vendor page, and none of those was transcribed, so none
    # may borrow a neighbour's rate. Bare gpt-6.1/6.2 must not alias Astra onto Sol's rate.
    for provider, model in (
        ("openai", "gpt-4o-mini-transcribe"),
        ("openai", "gpt-6.1"),
        ("openai", "gpt-6.2"),
        ("gemini", "gemini-2.5-flash-image"),
        ("deepseek", "deepseek-chat"),
    ):
        assert table.lookup(provider, model) is None, f"{provider}/{model}"


def test_deepseek_hit_and_miss_tokens_are_costed_at_their_own_rates() -> None:
    table = PricingRegistry.load_default().get_table("default-2026-11")
    assert table is not None
    identity = EndpointIdentity(
        "deepseek_chat",
        "deepseek",
        "deepseek-flash",
        "/chat/completions",
        pricing_table_id="default-2026-11",
    )
    usage = UsageObservation(
        state=UsageState.COMPLETE,
        buckets=TokenBuckets(4, 16, (), 9, 20, 0.8),
        raw_fields=freeze_json({}),
    )

    estimate = estimate_cost(identity, usage, table)

    assert estimate is not None
    assert estimate.estimated_input_cost_usd == pytest.approx(0.000001296)  # 4*0.30 + 16*0.006
    assert estimate.estimated_output_cost_usd == pytest.approx(0.0000108)  # 9*1.20
