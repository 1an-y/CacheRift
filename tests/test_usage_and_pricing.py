from __future__ import annotations

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
        "gpt-6.1",
        "/v1/responses",
        pricing_table_id="default-2026-11",
    )
    assert estimate_cost(known_id, usage_missing, table) is None
