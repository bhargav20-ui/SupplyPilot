"""Gemini-backed multi-agent orchestration. No silent LLM-to-rules fallback."""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .core import (
    assess_supply_risk,
    build_procurement_plan,
    calculate_inventory_exposure,
    forecast_demand,
    rank_suppliers,
    validate_procurement_plan,
)


class AgentRuntimeError(RuntimeError):
    def __init__(self, message: str, metrics: dict | None = None, timeline: list | None = None):
        super().__init__(message)
        self.metrics = metrics or {"gemini_calls": 0, "tool_calls": 0}
        self.timeline = timeline or []


PROMPT_OUTPUT = "Return only valid JSON. Do not use markdown fences. Do not invent facts, values, suppliers, SKUs, dates, or data sources. Distinguish a measured fact from an inference."


def _parse_json(text: str, agent_name: str) -> dict:
    if not text:
        raise AgentRuntimeError(f"{agent_name} returned an empty response.")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AgentRuntimeError(f"{agent_name} returned invalid JSON: {exc.msg}.")
    if not isinstance(value, dict):
        raise AgentRuntimeError(f"{agent_name} response must be a JSON object.")
    return value


def _normalize_output(value: dict, shape: dict) -> dict:
    """Coerce model JSON to the declared output types before downstream use."""
    normalized = {}
    for key, example in shape.items():
        raw = value.get(key)
        if isinstance(example, bool):
            if isinstance(raw, bool):
                normalized[key] = raw
            elif isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
                normalized[key] = raw.strip().lower() == "true"
            else:
                normalized[key] = False
        elif isinstance(example, list):
            items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) and raw.strip() else [])
            if example and isinstance(example[0], dict):
                normalized[key] = [item for item in items if isinstance(item, dict)]
            else:
                normalized[key] = [str(item) for item in items if item is not None]
        elif isinstance(example, str):
            normalized[key] = str(raw) if raw is not None and not isinstance(raw, (dict, list)) else ""
        else:
            normalized[key] = raw
    return normalized


def run_investigation(title: str, sales: list[dict], inventory: list[dict], suppliers: list[dict]) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise AgentRuntimeError("GEMINI_API_KEY is not configured. Add it to the environment and restart the app; no simulated AI findings were generated.")
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise AgentRuntimeError("The google-genai package is not installed. Run: python -m pip install -r requirements.txt") from exc
    client = genai.Client(api_key=api_key)
    counters = {"gemini_calls": 0, "tool_calls": 0}
    lock = threading.Lock()
    timeline: list[dict] = []

    def metrics_snapshot():
        with lock:
            return dict(counters)

    def tool(name: str, fn, *args):
        with lock:
            counters["tool_calls"] += 1
        started = time.perf_counter()
        result = fn(*args)
        timeline.append({"type": "tool", "name": name, "duration_ms": round((time.perf_counter() - started) * 1000), "status": "completed"})
        return result

    def ask_agent(name: str, role: str, evidence: dict, output_shape: dict) -> dict:
        prompt = f"""You are the {name} in SupplyPilot, a multi-agent supply-chain decision-support system.
ROLE: {role}
INCIDENT / BUSINESS QUESTION: {title}
ASSIGNED EVIDENCE AND TOOL OUTPUTS:
{json.dumps(evidence, ensure_ascii=False, default=str)}

Analyze only your assigned responsibility. Use concrete values from the evidence. If the evidence is insufficient, say so. Do not create purchase orders or claim actions were executed. Another agent will receive your findings, so make assumptions and caveats explicit.
REQUIRED JSON SHAPE (replace placeholders with evidence-based values):
{json.dumps(output_shape)}
The example values describe the required data types; do not return the placeholder words literally. Use JSON arrays for list fields and true/false JSON booleans for boolean fields. Use only the keys shown.
{PROMPT_OUTPUT}
"""
        last_error = None
        for attempt in range(3):
            started = time.perf_counter()
            try:
                with lock:
                    counters["gemini_calls"] += 1
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                        max_output_tokens=1800,
                    ),
                )
                result = _normalize_output(_parse_json(response.text or "", name), output_shape)
                timeline.append({"type": "agent", "name": name, "duration_ms": round((time.perf_counter() - started) * 1000), "status": "completed", "attempt": attempt + 1})
                return result
            except Exception as exc:  # preserve the provider error after transient retries
                last_error = exc
                message = str(exc).lower()
                transient = any(code in message for code in ("503", "unavailable", "429", "resource_exhausted", "500", "internal server error", "deadline"))
                if not transient or attempt == 2:
                    break
                time.sleep(1.25 * (attempt + 1))
        timeline.append({"type": "agent", "name": name, "duration_ms": 0, "status": "failed"})
        raise AgentRuntimeError(f"{name} failed after {attempt + 1} attempt(s): {last_error}", metrics_snapshot(), list(timeline))

    # Four independent specialists each call their own analytical tool, then reason over its output.
    forecast = tool("forecast_demand", forecast_demand, sales)
    exposure = tool("calculate_inventory_exposure", calculate_inventory_exposure, forecast, inventory)
    supplier_rankings = tool("rank_suppliers", rank_suppliers, suppliers)
    supply_risks = tool("assess_supply_risk", assess_supply_risk, inventory, supplier_rankings)

    specialist_specs = {
        "Demand Forecasting Agent": (
            "Analyze demand patterns, recent trend changes, forecast sensitivity, and which SKUs need closer attention. Do not perform inventory or supplier selection.",
            {"summary": "string", "key_findings": ["string"], "risks": ["string"], "forecast_confidence": "low|medium|high", "watchlist_skus": ["string"]},
            {"demand_forecast": forecast},
        ),
        "Inventory Risk Agent": (
            "Assess on-hand stock, days of cover, reorder points, safety stock, and time-to-stockout exposure. Explain which shortages are most urgent.",
            {"summary": "string", "key_findings": ["string"], "risks": ["string"], "critical_skus": ["string"], "assumptions": ["string"]},
            {"inventory_exposure": exposure},
        ),
        "Supplier Intelligence Agent": (
            "Compare supplier reliability, quality, cost, lead time, MOQ, and capacity. Explain trade-offs; do not optimize inventory levels.",
            {"summary": "string", "key_findings": ["string"], "risks": ["string"], "preferred_suppliers": [{"sku": "string", "supplier": "string", "reason": "string"}], "caveats": ["string"]},
            {"supplier_rankings": supplier_rankings},
        ),
        "Supply Risk Agent": (
            "Assess supplier concentration, alternate coverage, capacity limitations, and operational resilience per SKU. Identify dependency risks independent of price optimization.",
            {"summary": "string", "key_findings": ["string"], "risks": ["string"], "single_source_skus": ["string"], "mitigations": ["string"]},
            {"supply_risk": supply_risks},
        ),
    }
    findings: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {
            pool.submit(ask_agent, name, role, evidence, shape): name
            for name, (role, shape, evidence) in specialist_specs.items()
        }
        for future in as_completed(future_map):
            name = future_map[future]
            findings[name] = future.result()

    # The planner receives all four independent findings AND a constraint-checked numeric plan.
    plan = tool("generate_procurement_options", build_procurement_plan, exposure, supplier_rankings)
    hard_issues = tool("validate_procurement_plan", validate_procurement_plan, plan, supplier_rankings)
    planner = ask_agent(
        "Procurement Planning Agent",
        "Synthesize the four specialist agents. Explain the procurement strategy, sequence urgent SKUs first, compare cost with reliability and lead-time trade-offs, and make clear which orders are proposals only. The numeric plan is computed by a deterministic optimizer and must not be silently altered.",
        {"specialist_findings": findings, "constraint_checked_procurement_options": plan, "hard_constraint_issues": hard_issues},
        {"decision_summary": "string", "procurement_rationale": ["string"], "priority_actions": ["string"], "trade_offs": ["string"], "assumptions": ["string"]},
    )

    # Independent auditor depends on planner + all specialist output, not just the raw input.
    auditor = ask_agent(
        "Decision Auditor Agent",
        "Independently challenge the planner's proposed decisions. Check evidence traceability, supplier fit, lead-time exposure, capacity, MOQ, cost arithmetic, uncertainty, and whether any recommendation exceeds the supplied evidence. Do not rewrite the plan.",
        {"planner_output": planner, "specialist_findings": findings, "procurement_options": plan, "deterministic_constraint_issues": hard_issues},
        {"approved_for_human_review": "boolean", "concerns": ["string"], "checks": [{"check": "string", "result": "pass|warning|fail", "evidence": "string"}], "required_human_actions": ["string"]},
    )

    model_approved = bool(auditor.get("approved_for_human_review", False))
    approved = model_approved and not hard_issues
    risk_levels = [x["risk_level"] for x in exposure] + [x["risk_level"] for x in supply_risks]
    overall_risk = "CRITICAL" if hard_issues or plan["shortages"] else (
        "HIGH" if any(level in ("CRITICAL", "HIGH") for level in risk_levels) else
        "MEDIUM" if any(level == "MEDIUM" for level in risk_levels) else "LOW"
    )
    timeline.sort(key=lambda x: x.get("duration_ms", 0), reverse=True)
    return {
        "title": title,
        "model": model,
        "status": "ready_for_human_review" if approved else "review_required",
        "overall_risk": overall_risk,
        "agents": findings,
        "planner": planner,
        "auditor": auditor,
        "metrics": {
            "gemini_calls": counters["gemini_calls"],
            "tool_calls": counters["tool_calls"],
            "completed_agents": 6,
            "specialist_agents": 4,
        },
        "data": {
            "forecasts": forecast,
            "inventory_exposure": exposure,
            "supplier_rankings": supplier_rankings,
            "supply_risk": supply_risks,
            "procurement_plan": plan,
            "hard_constraint_issues": hard_issues,
        },
        "timeline": timeline,
        "disclaimer": "Decision support only. Review the uploaded data, assumptions, supplier terms, and recommendations before issuing any purchase order. SupplyPilot does not place orders or connect to live ERP systems.",
    }