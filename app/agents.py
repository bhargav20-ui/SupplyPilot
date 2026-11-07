"""Gemini-backed multi-agent orchestration. No silent LLM-to-rules fallback."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from .core import (
    assess_supply_risk,
    build_procurement_plan,
    calculate_inventory_exposure,
    forecast_demand,
    rank_suppliers,
    validate_procurement_plan,
)


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        metrics: dict | None = None,
        timeline: list | None = None,
        quota_reset_seconds: int | None = None,
    ):
        super().__init__(message)
        self.metrics = metrics or {"gemini_calls": 0, "tool_calls": 0}
        self.timeline = timeline or []
        self.quota_reset_seconds = quota_reset_seconds


PROMPT_OUTPUT = (
    "Return only the JSON object required by the response schema. Keep the whole response brief. "
    "Use at most 2 short items in each list. Do not invent facts, values, suppliers, SKUs, dates, or sources. "
    "Distinguish measured facts from inferences."
)


def _response_schema(shape: dict) -> dict:
    """Convert compact output-shape declarations into a strict JSON schema."""
    def object_schema(fields: dict) -> dict:
        properties = {key: field_schema(value) for key, value in fields.items()}
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    def field_schema(example: Any) -> dict:
        if isinstance(example, bool):
            return {"type": "boolean"}
        if isinstance(example, str):
            if "|" in example:
                return {"type": "string", "enum": [part.strip() for part in example.split("|")]}
            return {"type": "string"}
        if isinstance(example, list):
            if example and isinstance(example[0], dict):
                item = object_schema(example[0])
            else:
                item = {"type": "string"}
            return {"type": "array", "items": item, "maxItems": 3}
        if isinstance(example, dict):
            return object_schema(example)
        return {"type": "string"}

    return object_schema(shape)


def _is_daily_quota_error(error: Exception) -> bool:
    """Identify hard daily project/model quotas that cannot recover by retrying."""
    message = str(error).lower()
    return any(marker in message for marker in (
        "requests per day",
        "request per day",
        "per day limit exceeded",
        "generaterequestsperday",
        "daily quota",
        "generate_content_free_tier_requests",
    ))


def _is_rate_limit_error(error: Exception) -> bool:
    """Identify short-window rate limits; daily quotas are deliberately excluded."""
    message = str(error).lower()
    return any(marker in message for marker in (
        "requests per minute",
        "request per minute",
        "tokens per minute",
        "rate limit per minute",
        "retrydelay",
    )) and not _is_daily_quota_error(error)


def _retry_delay_seconds(error: Exception) -> int:
    """Read Google's RetryInfo without sleeping inside a worker thread."""
    import re

    message = str(error)
    match = re.search(r"retryDelay\s*['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s", message, re.IGNORECASE)
    if match:
        return max(60, int(float(match.group(1))))
    match = re.search(r"retry in\s+(?:(\d+)m)?(\d+(?:\.\d+)?)s", message, re.IGNORECASE)
    if match:
        minutes = int(match.group(1) or 0)
        return max(60, minutes * 60 + int(float(match.group(2))))
    return 3600


def _friendly_quota_error(model: str, error: Exception, retry_seconds: int) -> str:
    minutes = max(1, round(retry_seconds / 60))
    return (
        f"Gemini's daily request quota for {model} has been exhausted. "
        "SupplyPilot has stopped further Gemini retries to protect your quota. "
        f"New investigations are paused for about {minutes} minute(s). "
        "Check Google AI Studio → Dashboard → Usage and Rate limits. "
        "Wait for the quota reset, or enable a paid Gemini API tier with a spending limit."
    )


def _finish_reason(response: Any) -> str | None:
    """Return a provider finish reason when the SDK exposes one."""
    try:
        candidates = getattr(response, "candidates", None) or []
        reason = getattr(candidates[0], "finish_reason", None) if candidates else None
        if reason is None:
            return None
        return getattr(reason, "name", str(reason)).split(".")[-1]
    except Exception:
        return None


def _parse_json(text: str, agent_name: str, finish_reason: str | None = None) -> dict:
    if not text:
        suffix = f" Finish reason: {finish_reason}." if finish_reason else ""
        raise AgentRuntimeError(f"{agent_name} returned an empty response.{suffix}")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        suffix = f" Finish reason: {finish_reason}." if finish_reason else ""
        raise AgentRuntimeError(
            f"{agent_name} returned invalid JSON: {exc.msg} "
            f"(response length {len(cleaned)} characters).{suffix}"
        )
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


def run_investigation(
    title: str,
    sales: list[dict],
    inventory: list[dict],
    suppliers: list[dict],
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict:
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
    daily_quota_exhausted = threading.Event()
    provider_unavailable = threading.Event()

    def report_progress(name: str, status: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(name, status)
        except Exception:
            # Progress reporting must never fail an otherwise valid investigation.
            pass

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
        report_progress(name, "running")
        prompt = f"""You are the {name} in SupplyPilot, a multi-agent supply-chain decision-support system.
ROLE: {role}
BUSINESS QUESTION: {title}
ASSIGNED EVIDENCE AND TOOL OUTPUTS:
{json.dumps(evidence, ensure_ascii=False, default=str)}

Analyze only your assigned responsibility. Use concrete values from the evidence. If evidence is insufficient, say so. Do not create purchase orders or claim actions were executed. Keep the response concise so it can be checked and passed to another agent.
{PROMPT_OUTPUT}
"""
        schema = _response_schema(output_shape)
        last_error = None
        agent_quota_reset_seconds = None
        # Malformed JSON is retried once only. Provider outages also get at most one retry.
        for attempt in range(2):
            if daily_quota_exhausted.is_set():
                last_error = AgentRuntimeError(
                    f"{name} was not started because Gemini's daily quota was already exhausted."
                )
                break
            if provider_unavailable.is_set():
                last_error = AgentRuntimeError(
                    f"{name} was not started because Gemini remained temporarily unavailable after a previous agent."
                )
                break
            started = time.perf_counter()
            try:
                with lock:
                    counters["gemini_calls"] += 1
                request_prompt = prompt
                if attempt > 0:
                    request_prompt += "\nThe previous output could not be parsed. Return a shorter response with at most 2 list items per field. Follow the supplied JSON schema exactly; do not add commentary."
                response = client.models.generate_content(
                    model=model,
                    contents=request_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=schema,
                        temperature=0.1 if attempt == 0 else 0.0,
                        # Allow enough room for valid structured output. Low thinking
                        # keeps the short agent response from being crowded out.
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                        max_output_tokens=2048 if attempt == 0 else 4096,
                    ),
                )
                parsed = getattr(response, "parsed", None)
                if isinstance(parsed, dict):
                    value = parsed
                else:
                    value = _parse_json(
                        getattr(response, "text", None) or "",
                        name,
                        finish_reason=_finish_reason(response),
                    )
                result = _normalize_output(value, output_shape)
                timeline.append({"type": "agent", "name": name, "duration_ms": round((time.perf_counter() - started) * 1000), "status": "completed", "attempt": attempt + 1})
                report_progress(name, "completed")
                return result
            except Exception as exc:
                message = str(exc).lower()
                if _is_daily_quota_error(exc):
                    daily_quota_exhausted.set()
                    agent_quota_reset_seconds = _retry_delay_seconds(exc)
                    last_error = RuntimeError(_friendly_quota_error(model, exc, agent_quota_reset_seconds))
                    break  # A daily request quota cannot be fixed by another immediate request.
                last_error = exc
                malformed_output = isinstance(exc, AgentRuntimeError) and any(
                    marker in message for marker in ("invalid json", "empty response", "must be a json object")
                )
                provider_5xx = any(code in message for code in ("503", "unavailable", "500", "internal server error", "deadline", "timeout"))
                short_rate_limit = _is_rate_limit_error(exc)
                # A 503 is provider overload, not quota exhaustion. Give it one
                # paced retry. If it remains unavailable, stop sending this job's
                # remaining specialist requests rather than amplifying the burst.
                if provider_5xx and attempt >= 1:
                    provider_unavailable.set()
                    break

                # Repair malformed output once. Short-window quota errors receive
                # one delayed retry. Daily quotas were handled above and never retry.
                should_retry = attempt == 0 and (malformed_output or provider_5xx or short_rate_limit)
                if not should_retry:
                    break
                if provider_5xx:
                    time.sleep(10.0)
                elif short_rate_limit:
                    time.sleep(min(60, max(10, _retry_delay_seconds(exc))))
        timeline.append({"type": "agent", "name": name, "duration_ms": 0, "status": "failed"})
        report_progress(name, "failed")
        raise AgentRuntimeError(
            f"{name} failed after {attempt + 1} attempt(s): {last_error}",
            metrics_snapshot(),
            list(timeline),
            quota_reset_seconds=agent_quota_reset_seconds,
        )

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
            "Compare supplier reliability, quality, cost, lead time, MOQ, and capacity. Return at most 2 concise preferred options as strings in the form 'SKU — supplier — reason'. Explain trade-offs; do not optimize inventory levels.",
            {"summary": "string", "key_findings": ["string"], "risks": ["string"], "preferred_suppliers": ["string"], "caveats": ["string"]},
            {"supplier_rankings": supplier_rankings},
        ),
        "Supply Risk Agent": (
            "Assess supplier concentration, alternate coverage, capacity limitations, and operational resilience per SKU. Identify dependency risks independent of price optimization.",
            {"summary": "string", "key_findings": ["string"], "risks": ["string"], "single_source_skus": ["string"], "mitigations": ["string"]},
            {"supply_risk": supply_risks},
        ),
    }
    # Keep specialist requests independent but paced one at a time. This avoids
    # sending four simultaneous requests into a shared, burst-limited model pool.
    findings: dict[str, dict] = {}
    specialist_errors = []
    quota_delays = []
    for name, (role, shape, evidence) in specialist_specs.items():
        if daily_quota_exhausted.is_set():
            message = f"{name}: skipped because the Gemini daily request quota is exhausted."
            report_progress(name, "skipped")
            specialist_errors.append(message)
            continue
        if provider_unavailable.is_set():
            message = f"{name}: skipped because Gemini returned repeated 503 UNAVAILABLE responses."
            report_progress(name, "skipped")
            specialist_errors.append(message)
            continue
        try:
            findings[name] = ask_agent(name, role, evidence, shape)
        except Exception as exc:
            specialist_errors.append(f"{name}: {exc}")
            if isinstance(exc, AgentRuntimeError) and exc.quota_reset_seconds:
                quota_delays.append(exc.quota_reset_seconds)

    if specialist_errors:
        raise AgentRuntimeError(
            "Specialist analysis did not complete: " + " | ".join(specialist_errors),
            metrics_snapshot(),
            list(timeline),
            quota_reset_seconds=max(quota_delays) if quota_delays else None,
        )

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
        {"approved_for_human_review": False, "concerns": ["string"], "checks": [{"check": "string", "result": "pass|warning|fail", "evidence": "string"}], "required_human_actions": ["string"]},
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