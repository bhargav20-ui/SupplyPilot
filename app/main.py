from __future__ import annotations

import csv
import os
import secrets
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
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class InvestigationRequest(BaseModel):
    title: str = Field(min_length=5, max_length=180)
    sales: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    suppliers: list[dict[str, Any]]


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


def _read_sample(name: str) -> list[dict]:
    path = BASE_DIR / "data" / "sample" / name
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "ai_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "model": os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        "access_key_required": bool(os.getenv("APP_ACCESS_KEY", "").strip()),
        "mode": "Gemini multi-agent · local analytics tools",
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
    access_key = os.getenv("APP_ACCESS_KEY", "").strip()
    if access_key:
        supplied_key = request.headers.get("x-app-key", "")
        if not secrets.compare_digest(supplied_key, access_key):
            raise HTTPException(status_code=401, detail="Workspace access key is missing or incorrect.")
    try:
        validate_datasets(payload.sales, payload.inventory, payload.suppliers)
        result = run_investigation(payload.title, payload.sales, payload.inventory, payload.suppliers)
        return result
    except DataValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentRuntimeError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "metrics": exc.metrics, "timeline": exc.timeline, "completed": False})
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