from pathlib import Path
import csv
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import (
    DataValidationError,
    assess_supply_risk,
    build_procurement_plan,
    calculate_inventory_exposure,
    forecast_demand,
    rank_suppliers,
    validate_datasets,
    validate_procurement_plan,
)


def sample_rows(name):
    path = Path(__file__).resolve().parents[1] / "data" / "sample" / name
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_sample_datasets_pass_validation():
    validate_datasets(sample_rows("sales_history.csv"), sample_rows("inventory.csv"), sample_rows("suppliers.csv"))


def test_forecast_returns_all_skus_and_nonnegative_values():
    forecasts = forecast_demand(sample_rows("sales_history.csv"))
    assert len(forecasts) == 4
    assert all(row["forecast_daily_units"] >= 0 for row in forecasts)
    assert all(row["forecast_7d_units"] == round(row["forecast_daily_units"] * 7, 1) for row in forecasts)


def test_recent_demand_spike_increases_forecast_over_previous_rate():
    sales = []
    for day in range(14):
        qty = 10 if day < 7 else 20
        sales.append({"date": f"2026-01-{day+1:02d}", "sku": "A", "product": "Alpha", "units_sold": qty})
    result = forecast_demand(sales)[0]
    assert result["forecast_daily_units"] > result["recent_daily_avg"]
    assert result["trend_pct"] == 100


def test_inventory_exposure_flags_short_cover_as_critical():
    forecasts = [{"sku": "A", "product": "Alpha", "forecast_daily_units": 10}]
    inventory = [{"sku": "A", "product": "Alpha", "on_hand": 20, "lead_time_days": 7, "safety_stock": 10}]
    result = calculate_inventory_exposure(forecasts, inventory)[0]
    assert result["risk_level"] == "CRITICAL"
    assert result["days_of_cover"] == 2
    assert result["recommended_reorder_qty"] > 0


def test_supplier_ranking_prefers_reliable_quality_and_low_cost():
    suppliers = [
        {"supplier": "Cheap", "sku": "A", "unit_cost": 5, "lead_time_days": 4, "on_time_rate": 0.7, "quality_rate": 0.8, "capacity_units": 100, "min_order_qty": 10},
        {"supplier": "Reliable", "sku": "A", "unit_cost": 6, "lead_time_days": 5, "on_time_rate": 0.99, "quality_rate": 0.99, "capacity_units": 100, "min_order_qty": 10},
    ]
    ranked = rank_suppliers(suppliers)
    assert ranked[0]["supplier"] == "Reliable"


def test_percent_supplier_rates_are_supported():
    suppliers = [{"supplier": "A", "sku": "X", "unit_cost": 2, "lead_time_days": 3, "on_time_rate": 97, "quality_rate": 99, "capacity_units": 50, "min_order_qty": 5}]
    assert rank_suppliers(suppliers)[0]["on_time_rate"] == 97


def test_supply_risk_identifies_single_source():
    inventory = [{"sku": "A", "on_hand": 10}]
    ranked = [{"sku": "A", "supplier": "Only", "score": 90, "capacity_units": 100, "on_time_rate": 99, "quality_rate": 99}]
    assert assess_supply_risk(inventory, ranked)[0]["risk_level"] == "HIGH"


def test_procurement_plan_obeys_supplier_moq_and_capacity():
    exposure = [{"sku": "A", "product": "Alpha", "recommended_reorder_qty": 27}]
    suppliers = [
        {"sku": "A", "supplier": "First", "capacity_units": 20, "min_order_qty": 10, "unit_cost": 3, "score": 95, "lead_time_days": 4},
        {"sku": "A", "supplier": "Second", "capacity_units": 20, "min_order_qty": 5, "unit_cost": 4, "score": 80, "lead_time_days": 6},
    ]
    plan = build_procurement_plan(exposure, suppliers)
    assert plan["total_units"] >= 27
    assert not validate_procurement_plan(plan, suppliers)
    for order in plan["orders"]:
        assert order["quantity"] <= order["capacity_limit"]
        assert order["quantity"] % order["minimum_order_qty"] == 0


def test_missing_supplier_creates_uncovered_shortage():
    plan = build_procurement_plan([{"sku": "A", "product": "Alpha", "recommended_reorder_qty": 17}], [])
    assert plan["orders"] == []
    assert plan["shortages"][0]["uncovered_units"] == 17


def test_validation_rejects_missing_required_columns():
    try:
        validate_datasets([{"sku": "A"}], [{"sku": "A"}], [{"sku": "A"}])
    except DataValidationError as exc:
        assert "missing columns" in str(exc)
    else:
        raise AssertionError("Expected missing-column validation to fail")


def test_validation_rejects_mixed_supplier_currencies():
    sales = [{"date":"2026-01-01","sku":"A","product":"Alpha","units_sold":1}]
    inventory = [{"sku":"A","product":"Alpha","on_hand":1,"lead_time_days":1,"safety_stock":0}]
    suppliers = [
        {"supplier":"A","sku":"A","unit_cost":1,"lead_time_days":1,"on_time_rate":1,"quality_rate":1,"capacity_units":10,"min_order_qty":1,"currency":"USD"},
        {"supplier":"B","sku":"A","unit_cost":1,"lead_time_days":1,"on_time_rate":1,"quality_rate":1,"capacity_units":10,"min_order_qty":1,"currency":"INR"},
    ]
    try:
        validate_datasets(sales, inventory, suppliers)
    except DataValidationError as exc:
        assert "multiple currencies" in str(exc)
    else:
        raise AssertionError("Expected mixed currency data to be rejected")


def test_agent_output_normalizes_boolean_string_false_safely():
    from app.agents import _normalize_output
    shape = {"approved_for_human_review": False, "concerns": ["<string>"]}
    normalized = _normalize_output({"approved_for_human_review": "false", "concerns": "Needs lead-time review"}, shape)
    assert normalized["approved_for_human_review"] is False
    assert normalized["concerns"] == ["Needs lead-time review"]