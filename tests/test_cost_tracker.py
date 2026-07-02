import json
import pytest
from router.cost_tracker import CostTracker, MODEL_PRICING


def make(tmp_path):
    return CostTracker(log_path=str(tmp_path / "cost.jsonl"), monthly_budget=20.0)


def test_sonnet5_in_pricing_table():
    assert "claude-sonnet-5" in MODEL_PRICING
    assert "claude-sonnet-4-6" not in MODEL_PRICING


def test_cache_aware_cost(tmp_path):
    t = make(tmp_path)
    cost = t.record(
        "claude-haiku-4-5", 1000, 100,
        cache_read_tokens=10_000, cache_creation_tokens=2_000,
    )
    # uncached in @ $1 + cache reads @ 0.1x + cache writes @ 1.25x + out @ $5, per MTok
    expected = (1000 * 1.0 + 10_000 * 1.0 * 0.1 + 2_000 * 1.0 * 1.25 + 100 * 5.0) / 1_000_000
    assert cost == pytest.approx(expected)


def test_backward_compatible_three_arg_call(tmp_path):
    t = make(tmp_path)
    cost = t.record("claude-haiku-4-5", 1000, 100)
    assert cost == pytest.approx((1000 * 1.0 + 100 * 5.0) / 1_000_000)


def test_entry_carries_source_and_cache_fields(tmp_path):
    t = make(tmp_path)
    t.record("claude-haiku-4-5", 10, 5, cache_read_tokens=7, source="proactive")
    entry = json.loads((tmp_path / "cost.jsonl").read_text().strip())
    assert entry["source"] == "proactive"
    assert entry["cache_read"] == 7
    assert entry["cache_write"] == 0


def test_monthly_spend_tolerates_old_format_lines(tmp_path):
    p = tmp_path / "cost.jsonl"
    p.write_text('{"ts": "2099-01-01T00:00:00", "model": "x", "in": 1, "out": 1, "cost": 0.5}\n')
    t = CostTracker(log_path=str(p), monthly_budget=20.0)
    assert t.get_monthly_spend() >= 0.0  # must not raise
