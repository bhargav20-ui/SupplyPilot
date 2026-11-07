from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_endpoint_reports_runtime_configuration_without_exposing_secret():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "ai_configured" in body
    assert "GEMINI_API_KEY" not in str(body)


def test_sample_endpoint_returns_three_datasets():
    response = client.get("/api/sample")
    assert response.status_code == 200
    body = response.json()
    assert len(body["sales"]) >= 100
    assert len(body["inventory"]) == 4
    assert len(body["suppliers"]) >= 8


def test_root_serves_dashboard():
    response = client.get("/")
    assert response.status_code == 200
    assert "SupplyPilot" in response.text
    assert "Run agent analysis" in response.text


def test_investigation_without_gemini_key_fails_visibly_without_fake_results(monkeypatch):
    import csv
    from pathlib import Path
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    root = Path(__file__).resolve().parents[1] / "data" / "sample"
    def read(name):
        with (root / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    response = client.post("/api/investigate", json={
        "title": "Test inventory replenishment decision",
        "sales": read("sales_history.csv"),
        "inventory": read("inventory.csv"),
        "suppliers": read("suppliers.csv"),
    })
    assert response.status_code == 503
    body = response.json()
    assert body["completed"] is False
    assert "GEMINI_API_KEY is not configured" in body["detail"]
    assert body["metrics"] == {"gemini_calls": 0, "tool_calls": 0}


def test_configured_workspace_key_blocks_anonymous_investigation(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEY", "test-workspace-secret")
    response = client.post("/api/investigate", json={
        "title": "Test investigation brief",
        "sales": [{"date":"2026-01-01","sku":"A","product":"Alpha","units_sold":1}],
        "inventory": [{"sku":"A","product":"Alpha","on_hand":1,"lead_time_days":1,"safety_stock":0}],
        "suppliers": [{"supplier":"S","sku":"A","unit_cost":1,"lead_time_days":1,"on_time_rate":1,"quality_rate":1,"capacity_units":10,"min_order_qty":1}],
    })
    assert response.status_code == 401
    assert "access key" in response.json()["detail"].lower()