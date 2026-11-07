"""Deterministic supply-chain analytics used as tools by the specialist agents."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from math import ceil
from typing import Any


class DataValidationError(ValueError):
    pass


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise DataValidationError(f"'{field}' must be numeric.")
    if number != number or number in (float("inf"), float("-inf")) or number < minimum:
        raise DataValidationError(f"'{field}' must be a finite number >= {minimum}.")
    return number


def _date(value: Any) -> date:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        raise DataValidationError(f"Invalid sales date '{value}'. Use YYYY-MM-DD.")


def validate_datasets(sales: list[dict], inventory: list[dict], suppliers: list[dict]) -> None:
    if not sales or not inventory or not suppliers:
        raise DataValidationError("Upload or load sample data for sales, inventory, and suppliers.")
    if len(sales) > 20000 or len(inventory) > 2000 or len(suppliers) > 5000:
        raise DataValidationError("Dataset is too large for this demo (sales 20,000; inventory 2,000; suppliers 5,000 rows max).")
    required = {
        "sales": (sales, {"date", "sku", "product", "units_sold"}),
        "inventory": (inventory, {"sku", "product", "on_hand", "lead_time_days", "safety_stock"}),
        "suppliers": (suppliers, {"supplier", "sku", "unit_cost", "lead_time_days", "on_time_rate", "quality_rate", "capacity_units", "min_order_qty"}),
    }
    for label, (rows, columns) in required.items():
        missing = columns - set(rows[0].keys())
        if missing:
            raise DataValidationError(f"{label.title()} data is missing columns: {', '.join(sorted(missing))}.")
    for row in sales:
        _date(row.get("date"))
        if not str(row.get("sku", "")).strip():
            raise DataValidationError("Every sales row must include a SKU.")
        _number(row.get("units_sold"), "units_sold")
    for row in inventory:
        if not str(row.get("sku", "")).strip():
            raise DataValidationError("Every inventory row must include a SKU.")
        for field in ("on_hand", "lead_time_days", "safety_stock"):
            _number(row.get(field), field)
    currencies = {str(row.get("currency", "USD") or "USD").strip().upper() for row in suppliers}
    if len(currencies) > 1:
        raise DataValidationError("Supplier data contains multiple currencies. Convert unit costs to one currency before analyzing.")
    if any(len(currency) != 3 or not currency.isalpha() for currency in currencies):
        raise DataValidationError("Supplier currency must be a three-letter code such as USD or INR.")
    for row in suppliers:
        if not str(row.get("supplier", "")).strip() or not str(row.get("sku", "")).strip():
            raise DataValidationError("Every supplier row must include supplier and SKU.")
        for field in ("unit_cost", "lead_time_days", "capacity_units"):
            _number(row.get(field), field)
        _number(row.get("min_order_qty"), "min_order_qty", minimum=1)
        for field in ("on_time_rate", "quality_rate"):
            rate = _number(row.get(field), field)
            if rate > 100:
                raise DataValidationError(f"'{field}' must be between 0 and 1, or 0 and 100.")


def _rate(value: Any) -> float:
    value = float(value)
    return value / 100.0 if value > 1 else value


def forecast_demand(sales: list[dict]) -> list[dict]:
    """Forecast per-SKU daily demand using recent-vs-prior 7-day run rates."""
    grouped: dict[str, list[tuple[date, float, str]]] = defaultdict(list)
    for row in sales:
        grouped[str(row["sku"])].append((_date(row["date"]), float(row["units_sold"]), str(row.get("product", row["sku"]))))
    results = []
    for sku, rows in grouped.items():
        latest = max(item[0] for item in rows)
        recent_start = latest - timedelta(days=6)
        prior_start = latest - timedelta(days=13)
        recent = sum(qty for day, qty, _ in rows if recent_start <= day <= latest) / 7
        prior = sum(qty for day, qty, _ in rows if prior_start <= day < recent_start) / 7
        growth = ((recent - prior) / prior * 100) if prior > 0 else (0.0 if recent == 0 else 100.0)
        # Damp the trend so a single volatile week cannot dominate the forecast.
        adjustment = max(-0.25, min(0.40, (growth / 100) * 0.35))
        forecast_daily = max(0.0, recent * (1 + adjustment))
        results.append({
            "sku": sku,
            "product": rows[-1][2],
            "latest_sales_date": latest.isoformat(),
            "recent_daily_avg": round(recent, 2),
            "previous_daily_avg": round(prior, 2),
            "trend_pct": round(growth, 1),
            "forecast_daily_units": round(forecast_daily, 2),
            "forecast_7d_units": round(round(forecast_daily, 2) * 7, 1),
            "method": "7-day run rate with damped recent trend",
        })
    return sorted(results, key=lambda item: item["sku"])


def calculate_inventory_exposure(forecasts: list[dict], inventory: list[dict]) -> list[dict]:
    forecast_by_sku = {item["sku"]: item for item in forecasts}
    results = []
    for row in inventory:
        sku = str(row["sku"])
        forecast = forecast_by_sku.get(sku, {})
        daily = float(forecast.get("forecast_daily_units", 0))
        on_hand = float(row["on_hand"])
        lead = float(row["lead_time_days"])
        safety = float(row["safety_stock"])
        cover = (on_hand / daily) if daily > 0 else None
        reorder_point = daily * lead + safety
        target_stock = daily * (lead + 7) + safety
        reorder_qty = max(0, ceil(target_stock - on_hand))
        if daily <= 0:
            risk = "UNKNOWN"
        elif cover < lead:
            risk = "CRITICAL"
        elif cover < lead + 3:
            risk = "HIGH"
        elif on_hand < reorder_point:
            risk = "MEDIUM"
        else:
            risk = "LOW"
        results.append({
            "sku": sku,
            "product": str(row.get("product", sku)),
            "on_hand": round(on_hand, 1),
            "forecast_daily_units": round(daily, 2),
            "days_of_cover": round(cover, 1) if cover is not None else None,
            "lead_time_days": round(lead, 1),
            "safety_stock": round(safety, 1),
            "reorder_point": round(reorder_point, 1),
            "target_stock": round(target_stock, 1),
            "recommended_reorder_qty": reorder_qty,
            "risk_level": risk,
        })
    return sorted(results, key=lambda item: ({"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}[item["risk_level"]], item["sku"]))


def rank_suppliers(suppliers: list[dict]) -> list[dict]:
    """Score suppliers per SKU: reliability 47%, quality 38%, unit-cost 15%."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in suppliers:
        groups[str(row["sku"])].append(row)
    results = []
    for sku, rows in groups.items():
        costs = [float(row["unit_cost"]) for row in rows]
        low, high = min(costs), max(costs)
        for row in rows:
            on_time = _rate(row["on_time_rate"])
            quality = _rate(row["quality_rate"])
            cost = float(row["unit_cost"])
            cost_score = 1.0 if high == low else (high - cost) / (high - low)
            score = 0.47 * on_time + 0.38 * quality + 0.15 * cost_score
            results.append({
                "sku": sku,
                "supplier": str(row["supplier"]),
                "unit_cost": round(cost, 2),
                "lead_time_days": round(float(row["lead_time_days"]), 1),
                "on_time_rate": round(on_time * 100, 1),
                "quality_rate": round(quality * 100, 1),
                "capacity_units": int(float(row["capacity_units"])),
                "min_order_qty": int(float(row["min_order_qty"])),
                "score": round(score * 100, 1),
            })
    return sorted(results, key=lambda item: (item["sku"], -item["score"]))


def assess_supply_risk(inventory: list[dict], ranked_suppliers: list[dict]) -> list[dict]:
    by_sku: dict[str, list[dict]] = defaultdict(list)
    for supplier in ranked_suppliers:
        by_sku[supplier["sku"]].append(supplier)
    results = []
    for item in inventory:
        sku = str(item["sku"])
        options = by_sku.get(sku, [])
        capacity = sum(option["capacity_units"] for option in options)
        best = options[0] if options else None
        if not options:
            risk, reason = "CRITICAL", "No supplier records are available for this SKU."
        elif len(options) == 1:
            risk, reason = "HIGH", "Only one supplier is listed; there is no qualified alternate in the supplied data."
        elif best["on_time_rate"] < 85 or best["quality_rate"] < 90:
            risk, reason = "HIGH", "The highest-scoring supplier has a weak reliability or quality metric."
        elif capacity < float(item["on_hand"]) * 0.25:
            risk, reason = "MEDIUM", "Recorded supplier capacity is limited relative to current inventory."
        else:
            risk, reason = "LOW", "Multiple supplier options are recorded and the leading option clears basic reliability checks."
        results.append({
            "sku": sku,
            "supplier_count": len(options),
            "combined_recorded_capacity": capacity,
            "best_supplier": best["supplier"] if best else None,
            "best_supplier_score": best["score"] if best else None,
            "risk_level": risk,
            "reason": reason,
        })
    return sorted(results, key=lambda item: ({"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}[item["risk_level"]], item["sku"]))


def build_procurement_plan(exposure: list[dict], ranked_suppliers: list[dict]) -> dict:
    """Create MOQ/capacity-constrained order options; no purchase is executed."""
    supplier_groups: dict[str, list[dict]] = defaultdict(list)
    for supplier in ranked_suppliers:
        supplier_groups[supplier["sku"]].append(supplier)
    orders, shortages = [], []
    for item in exposure:
        remaining = int(item["recommended_reorder_qty"])
        if remaining <= 0:
            continue
        sku = item["sku"]
        candidates = supplier_groups.get(sku, [])
        if not candidates:
            shortages.append({"sku": sku, "uncovered_units": remaining, "reason": "No supplier is available for this SKU."})
            continue
        sku_orders = []
        for supplier in candidates:
            if remaining <= 0:
                break
            capacity = int(supplier["capacity_units"])
            moq = max(1, int(supplier["min_order_qty"]))
            usable_capacity = (capacity // moq) * moq
            if usable_capacity <= 0:
                continue
            desired = min(remaining, usable_capacity)
            quantity = min(usable_capacity, int(ceil(desired / moq) * moq))
            if quantity < moq:
                continue
            sku_orders.append({
                "sku": sku,
                "product": item["product"],
                "supplier": supplier["supplier"],
                "quantity": quantity,
                "unit_cost": supplier["unit_cost"],
                "estimated_cost": round(quantity * supplier["unit_cost"], 2),
                "supplier_score": supplier["score"],
                "lead_time_days": supplier["lead_time_days"],
                "capacity_limit": capacity,
                "minimum_order_qty": moq,
            })
            remaining = max(0, remaining - quantity)
        orders.extend(sku_orders)
        if remaining > 0:
            shortages.append({"sku": sku, "uncovered_units": remaining, "reason": "Qualified supplier capacity or MOQ constraints prevent covering the full reorder quantity."})
    estimated_cost = round(sum(order["estimated_cost"] for order in orders), 2)
    currencies = {str(item.get("currency", "USD")).upper() for item in ranked_suppliers}
    if len(currencies) > 1:
        raise DataValidationError("Supplier data contains multiple currencies. Convert unit costs to one currency before analyzing.")
    currency = next(iter(currencies), "USD")
    return {
        "orders": orders,
        "shortages": shortages,
        "total_order_lines": len(orders),
        "total_units": sum(order["quantity"] for order in orders),
        "estimated_cost": estimated_cost,
        "currency": currency,
        "note": "Planning estimate only. Currency and costs reflect the uploaded supplier file; no purchase order is placed.",
    }


def validate_procurement_plan(plan: dict, ranked_suppliers: list[dict]) -> list[str]:
    issues = []
    supplier_map = {(item["sku"], item["supplier"]): item for item in ranked_suppliers}
    for order in plan.get("orders", []):
        supplier = supplier_map.get((order["sku"], order["supplier"]))
        if not supplier:
            issues.append(f"{order['sku']}: selected supplier is not present in the supplier dataset.")
            continue
        if order["quantity"] <= 0 or order["quantity"] > supplier["capacity_units"]:
            issues.append(f"{order['sku']}: order quantity violates the supplier capacity constraint.")
        if order["quantity"] < supplier["min_order_qty"] or order["quantity"] % supplier["min_order_qty"] != 0:
            issues.append(f"{order['sku']}: order quantity violates the supplier minimum-order multiple.")
    return issues