from __future__ import annotations

import csv
import io
import os
import threading
import time
from collections import defaultdict, deque
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
    version="1.1.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class InvestigationRequest(BaseModel):
    title: str = Field(min_length=5, max_length=180)
    sales: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    suppliers: list[dict[str, Any]]


# Public guest access: limit expensive model runs without asking visitors to share
# an administrator secret. This limiter is intentionally process-local; see README
# for the limitation and the recommended persistent limiter for a larger deployment.
_RATE_LOCK = threading.Lock()
_RUN_HISTORY: dict[str, deque[float]] = defaultdict(deque)
_ACTIVE_RUNS = 0


def _public_limits() -> tuple[int, int, int]:
    return (
        max(1, int(os.getenv("PUBLIC_RUN_LIMIT", "5"))),
        max(60, int(os.getenv("PUBLIC_RUN_WINDOW_SECONDS", "900"))),
        max(1, int(os.getenv("PUBLIC_MAX_CONCURRENT_RUNS", "2"))),
    )


def _client_key(request: Request) -> str:
    # Render's edge proxy forwards the visitor IP in X-Forwarded-For. The first
    # address identifies the original visitor; request.client is a local fallback.
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


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


_SAMPLE_REQUIRED_COLUMNS = {
    "sales_history.csv": {"date", "sku", "product", "units_sold"},
    "inventory.csv": {"sku", "product", "on_hand", "lead_time_days", "safety_stock"},
    "suppliers.csv": {"supplier", "sku", "unit_cost", "lead_time_days", "on_time_rate", "quality_rate", "capacity_units", "min_order_qty"},
}


def _sample_header(value: str) -> str:
    """Normalize CSV column labels and ignore spreadsheet-generated index columns."""
    label = str(value or "").replace("\ufeff", "").strip().lower()
    label = "_".join(label.replace("-", " ").split())
    if label in {"index", "row_number", "unnamed:_0", "unnamed_0"}:
        return "__index__"
    return label


def _read_sample(name: str) -> list[dict]:
    """Read only the real header/table, tolerating spreadsheet export preambles.

    Some CSV export/parsing paths prepend a sheet title and an index column. The
    previous DictReader implementation treated the title as the header, causing
    every required column to appear missing. Locate the actual schema row instead.
    """
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
        # Skip repeated header rows sometimes introduced by CSV exports.
        padded = (values + [""] * len(headers))[:len(headers)]
        if all(not key.startswith("__") and str(value).strip().lower() == key for key, value in zip(headers, padded) if key):
            continue
        row = {}
        for key, value in zip(headers, padded):
            if key and not key.startswith("__"):
                row[key] = value.strip()
        if required.issubset(row.keys()):
            records.append(row)

    if not records:
        raise ValueError(f"Sample dataset {name} contains a header but no valid data rows.")
    return records


@app.get("/api/health")
def health():
    limit, window_seconds, max_concurrent = _public_limits()
    return {
        "status": "ok",
        "ai_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "model": os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        "public_access": True,
        "rate_limit": {"runs": limit, "window_seconds": window_seconds},
        "max_concurrent_runs": max_concurrent,
        "mode": "Public guest access · Gemini multi-agent · local analytics tools",
    }


@app.get("/api/sample")
def sample_data():
    return {
        "sales": _read_sample("sales_history.csv"),
        "inventory": _read_sample("inventory.csv"),
        "suppliers": _read_sample("suppliers.csv"),
        "label": "Synthetic demonstration data — not live company data",
    }


@app.post("/api/investigate")
def investigate(payload: InvestigationRequest, request: Request):
    try:
        validate_datasets(payload.sales, payload.inventory, payload.suppliers)
        _reserve_public_run(request)
        try:
            return run_investigation(payload.title, payload.sales, payload.inventory, payload.suppliers)
        finally:
            _release_public_run()
    except DataValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentRuntimeError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "metrics": exc.metrics, "timeline": exc.timeline, "completed": False})
    except HTTPException:
        raise
    except Exception as exc:
        # Expose a useful failure without pretending the run completed.
        raise HTTPException(status_code=500, detail=f"Investigation failed: {type(exc).__name__}: {exc}") from exc


@app.get("/api/scenarios")
def scenarios():
    return [
        {"id": "demand-spike", "title": "Demand spike across high-velocity SKUs", "description": "A recent increase in sales may outpace current inventory and supplier lead times."},
        {"id": "single-source", "title": "Single-source supplier exposure", "description": "Several products depend on one supplier with constrained capacity."},
        {"id": "cost-vs-reliability", "title": "Lowest-cost supplier has weaker reliability", "description": "Compare unit price savings against delivery and quality performance."},
    ]