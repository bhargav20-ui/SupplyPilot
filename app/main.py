from __future__ import annotations

import csv
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents import AgentRuntimeError, run_investigation
from .core import DataValidationError, validate_datasets

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

app = FastAPI(
    title="SupplyPilot",
    description="Multi-agent supply-chain decision intelligence",
    version="1.2.1",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
logger = logging.getLogger("supplypilot")


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    """Keep application-level failures JSON-shaped for API callers."""
    error_id = uuid.uuid4().hex[:12]
    logger.exception("Unhandled request error id=%s path=%s", error_id, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "SupplyPilot encountered an unexpected server error. Please retry. If it continues, share this error ID with the administrator.",
            "error_id": error_id,
        },
        headers={"Cache-Control": "no-store"},
    )


class InvestigationRequest(BaseModel):
    title: str = Field(min_length=5, max_length=180)
    sales: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    suppliers: list[dict[str, Any]]


AGENT_NAMES = [
    "Demand Forecasting Agent",
    "Inventory Risk Agent",
    "Supplier Intelligence Agent",
    "Supply Risk Agent",
    "Procurement Planning Agent",
    "Decision Auditor Agent",
]

# Public guest access and process-local job/rate-limit state. This is appropriate
# for the single-instance demo; use Redis/Render Key Value for multi-instance scale.
_RATE_LOCK = threading.Lock()
_RUN_HISTORY: dict[str, deque[float]] = defaultdict(deque)
_ACTIVE_RUNS = 0
_GEMINI_QUOTA_LOCK = threading.Lock()
_GEMINI_QUOTA_PAUSED_UNTIL = 0.0
_TASK_LOCK = threading.Lock()
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_TTL_SECONDS = 2 * 60 * 60
_TASK_CACHE_LIMIT = 150
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="supplypilot-agent")


def _public_limits() -> tuple[int, int, int]:
    return (
        max(1, int(os.getenv("PUBLIC_RUN_LIMIT", "5"))),
        max(60, int(os.getenv("PUBLIC_RUN_WINDOW_SECONDS", "900"))),
        max(1, int(os.getenv("PUBLIC_MAX_CONCURRENT_RUNS", "2"))),
    )


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",", 1)[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _reserve_public_run(request: Request) -> None:
    global _ACTIVE_RUNS
    limit, window_seconds, max_concurrent = _public_limits()
    client_key = _client_key(request)
    now = time.monotonic()
    with _RATE_LOCK:
        history = _RUN_HISTORY[client_key]
        while history and now - history[0] >= window_seconds:
            history.popleft()
        if len(history) >= limit:
            retry_after = max(1, int(window_seconds - (now - history[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Guest limit reached. Please try again in about {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )
        if _ACTIVE_RUNS >= max_concurrent:
            raise HTTPException(
                status_code=503,
                detail="SupplyPilot is handling other investigations right now. Please retry shortly.",
                headers={"Retry-After": "30"},
            )
        history.append(now)
        _ACTIVE_RUNS += 1


def _release_public_run() -> None:
    global _ACTIVE_RUNS
    with _RATE_LOCK:
        _ACTIVE_RUNS = max(0, _ACTIVE_RUNS - 1)


def _pause_for_gemini_quota(seconds: int) -> None:
    """Open a process-wide circuit breaker after Gemini reports a hard daily quota."""
    global _GEMINI_QUOTA_PAUSED_UNTIL
    with _GEMINI_QUOTA_LOCK:
        _GEMINI_QUOTA_PAUSED_UNTIL = max(
            _GEMINI_QUOTA_PAUSED_UNTIL,
            time.time() + max(60, seconds),
        )


def _gemini_quota_pause_remaining() -> int:
    with _GEMINI_QUOTA_LOCK:
        return max(0, int(_GEMINI_QUOTA_PAUSED_UNTIL - time.time()))


def _prune_tasks() -> None:
    now = time.time()
    with _TASK_LOCK:
        expired = [
            task_id for task_id, task in _TASKS.items()
            if task["status"] in {"completed", "failed"} and now - task["updated_at"] > _TASK_TTL_SECONDS
        ]
        for task_id in expired:
            _TASKS.pop(task_id, None)
        if len(_TASKS) > _TASK_CACHE_LIMIT:
            completed = sorted(
                ((task_id, task["updated_at"]) for task_id, task in _TASKS.items() if task["status"] in {"completed", "failed"}),
                key=lambda item: item[1],
            )
            for task_id, _ in completed[: max(0, len(_TASKS) - _TASK_CACHE_LIMIT)]:
                _TASKS.pop(task_id, None)


def _task_snapshot(task_id: str) -> dict[str, Any]:
    _prune_tasks()
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="This investigation task was not found or has expired. Start a new analysis.")
        # Return a shallow copy to avoid serializing a dictionary while a worker mutates it.
        snapshot = dict(task)
        snapshot["agents"] = dict(task["agents"])
        return snapshot


def _set_agent_progress(task_id: str, agent_name: str, status: str) -> None:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is not None and agent_name in task["agents"]:
            task["agents"][agent_name] = status
            task["updated_at"] = time.time()


def _execute_task(task_id: str, title: str, sales: list[dict], inventory: list[dict], suppliers: list[dict]) -> None:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is not None:
            task["status"] = "running"
            task["updated_at"] = time.time()

    def progress(agent_name: str, status: str) -> None:
        _set_agent_progress(task_id, agent_name, status)

    try:
        result = run_investigation(
            title,
            sales,
            inventory,
            suppliers,
            progress_callback=progress,
        )
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "completed"
                task["result"] = result
                task["metrics"] = result.get("metrics", {})
                task["updated_at"] = time.time()
    except AgentRuntimeError as exc:
        if exc.quota_reset_seconds:
            _pause_for_gemini_quota(exc.quota_reset_seconds)
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "failed"
                task["error"] = str(exc)
                task["metrics"] = exc.metrics
                task["timeline"] = exc.timeline
                task["updated_at"] = time.time()
                for name, current in task["agents"].items():
                    if current in {"queued", "running"}:
                        task["agents"][name] = "not completed"
    except Exception as exc:  # unexpected errors are recorded instead of killing the request path
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "failed"
                task["error"] = f"Investigation failed: {type(exc).__name__}: {exc}"
                task["updated_at"] = time.time()
                for name, current in task["agents"].items():
                    if current in {"queued", "running"}:
                        task["agents"][name] = "not completed"
    finally:
        _release_public_run()


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


_SAMPLE_REQUIRED_COLUMNS = {
    "sales_history.csv": {"date", "sku", "product", "units_sold"},
    "inventory.csv": {"sku", "product", "on_hand", "lead_time_days", "safety_stock"},
    "suppliers.csv": {"supplier", "sku", "unit_cost", "lead_time_days", "on_time_rate", "quality_rate", "capacity_units", "min_order_qty"},
}


def _sample_header(value: str) -> str:
    label = str(value or "").replace("\ufeff", "").strip().lower()
    label = "_".join(label.replace("-", " ").split())
    if label in {"index", "row_number", "unnamed:_0", "unnamed_0"}:
        return "__index__"
    return label


def _read_sample(name: str) -> list[dict]:
    path = BASE_DIR / "data" / "sample" / name
    required = _SAMPLE_REQUIRED_COLUMNS.get(name)
    if not required:
        raise ValueError(f"Unknown sample dataset: {name}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.reader(handle))
    header_index = None
    normalized_header = []
    for index, candidate in enumerate(raw_rows[:20]):
        labels = [_sample_header(value) for value in candidate]
        if required.issubset(set(labels)):
            header_index = index
            normalized_header = labels
            break
    if header_index is None:
        raise ValueError(f"Could not locate the required header row in sample dataset {name}.")
    headers = normalized_header
    records: list[dict] = []
    for values in raw_rows[header_index + 1:]:
        if not values or not any(str(value).strip() for value in values):
            continue
        padded = (values + [""] * len(headers))[:len(headers)]
        if all(not key.startswith("__") and str(value).strip().lower() == key for key, value in zip(headers, padded) if key):
            continue
        row = {key: value.strip() for key, value in zip(headers, padded) if key and not key.startswith("__")}
        if required.issubset(row.keys()):
            records.append(row)
    if not records:
        raise ValueError(f"Sample dataset {name} contains a header but no valid data rows.")
    return records


@app.get("/api/health")
def health():
    limit, window_seconds, max_concurrent = _public_limits()
    quota_wait = _gemini_quota_pause_remaining()
    return {
        "status": "ok",
        "version": app.version,
        "ai_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "model": os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        "public_access": True,
        "rate_limit": {"runs": limit, "window_seconds": window_seconds},
        "max_concurrent_runs": max_concurrent,
        "gemini_quota_paused": quota_wait > 0,
        "gemini_quota_retry_after_seconds": quota_wait,
        "mode": "Public guest access · asynchronous Gemini multi-agent jobs",
    }


@app.get("/api/sample")
def sample_data():
    return {
        "sales": _read_sample("sales_history.csv"),
        "inventory": _read_sample("inventory.csv"),
        "suppliers": _read_sample("suppliers.csv"),
        "label": "Synthetic demonstration data — not live company data",
    }


@app.post("/api/investigate", status_code=202)
def investigate(payload: InvestigationRequest, request: Request):
    """Validate and enqueue an investigation; return promptly before Gemini calls run."""
    try:
        quota_wait = _gemini_quota_pause_remaining()
        if quota_wait:
            raise HTTPException(
                status_code=429,
                detail=(
                    "Gemini's daily request quota was exhausted recently. "
                    f"SupplyPilot has paused new investigations for about {max(1, round(quota_wait / 60))} minutes "
                    "to avoid wasting more API requests. Check Google AI Studio Usage and Rate limits."
                ),
                headers={"Retry-After": str(quota_wait)},
            )
        validate_datasets(payload.sales, payload.inventory, payload.suppliers)
        _reserve_public_run(request)
        task_id = uuid.uuid4().hex
        now = time.time()
        task = {
            "task_id": task_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "agents": {name: "queued" for name in AGENT_NAMES},
            "result": None,
            "error": None,
            "metrics": {"gemini_calls": 0, "tool_calls": 0},
            "timeline": [],
        }
        with _TASK_LOCK:
            _TASKS[task_id] = task
        try:
            _EXECUTOR.submit(
                _execute_task,
                task_id,
                payload.title,
                payload.sales,
                payload.inventory,
                payload.suppliers,
            )
        except Exception:
            with _TASK_LOCK:
                _TASKS.pop(task_id, None)
            _release_public_run()
            raise
        return JSONResponse(
            status_code=202,
            content={"task_id": task_id, "status": "queued", "status_url": f"/api/investigations/{task_id}"},
            headers={"Cache-Control": "no-store"},
        )
    except DataValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start investigation: {type(exc).__name__}: {exc}") from exc


@app.get("/api/investigations/{task_id}")
def investigation_status(task_id: str):
    snapshot = _task_snapshot(task_id)
    payload = {
        "task_id": snapshot["task_id"],
        "status": snapshot["status"],
        "agents": snapshot["agents"],
        "updated_at": snapshot["updated_at"],
        "metrics": snapshot["metrics"],
    }
    if snapshot["status"] == "completed":
        payload["result"] = snapshot["result"]
    elif snapshot["status"] == "failed":
        payload["error"] = snapshot["error"] or "The investigation could not complete."
        payload["timeline"] = snapshot.get("timeline", [])
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@app.get("/api/scenarios")
def scenarios():
    return [
        {"id": "demand-spike", "title": "Demand spike across high-velocity SKUs", "description": "A recent increase in sales may outpace current inventory and supplier lead times."},
        {"id": "single-source", "title": "Single-source supplier exposure", "description": "Several products depend on one supplier with constrained capacity."},
        {"id": "cost-vs-reliability", "title": "Lowest-cost supplier has weaker reliability", "description": "Compare unit price savings against delivery and quality performance."},
    ]