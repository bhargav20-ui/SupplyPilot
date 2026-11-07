from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app, _RATE_LOCK, _RUN_HISTORY

client = TestClient(app)


def sample_payload():
    return {
        "title": "Test inventory replenishment decision",
        "sales": [{"date": "2026-01-01", "sku": "A", "product": "Alpha", "units_sold": 1}],
        "inventory": [{"sku": "A", "product": "Alpha", "on_hand": 1, "lead_time_days": 1, "safety_stock": 0}],
        "suppliers": [{"supplier": "S", "sku": "A", "unit_cost": 1, "lead_time_days": 1, "on_time_rate": 1, "quality_rate": 1, "capacity_units": 10, "min_order_qty": 1}],
    }


def clear_guest_limit(ip):
    with _RATE_LOCK:
        _RUN_HISTORY.pop(ip, None)


def test_health_endpoint_reports_public_runtime_without_exposing_secret(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEY", "legacy-private-key")
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["public_access"] is True
    assert body["rate_limit"]["runs"] >= 1
    assert "legacy-private-key" not in str(body)
    assert "GEMINI_API_KEY" not in str(body)


def test_sample_endpoint_returns_three_datasets():
    response = client.get("/api/sample")
    assert response.status_code == 200
    body = response.json()
    assert len(body["sales"]) >= 100
    assert len(body["inventory"]) == 4
    assert len(body["suppliers"]) >= 8


def test_root_serves_public_dashboard_without_workspace_key_field():
    response = client.get("/")
    assert response.status_code == 200
    assert "SupplyPilot" in response.text
    assert "Run analysis" in response.text
    assert "workspace-key" not in response.text


def test_investigation_without_gemini_key_fails_visibly_without_fake_results(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ip = "198.51.100.10"
    clear_guest_limit(ip)
    response = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    assert response.status_code == 503
    body = response.json()
    assert body["completed"] is False
    assert "GEMINI_API_KEY is not configured" in body["detail"]
    assert body["metrics"] == {"gemini_calls": 0, "tool_calls": 0}


def test_public_guest_can_run_without_shared_access_key(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEY", "legacy-private-key")
    monkeypatch.setenv("PUBLIC_RUN_LIMIT", "5")
    ip = "198.51.100.11"
    clear_guest_limit(ip)
    monkeypatch.setattr("app.main.run_investigation", lambda *args: {"completed": True, "status": "test"})
    response = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    assert response.status_code == 200
    assert response.json()["completed"] is True


def test_public_guest_rate_limit_rejects_excess_runs(monkeypatch):
    monkeypatch.setenv("PUBLIC_RUN_LIMIT", "1")
    monkeypatch.setenv("PUBLIC_RUN_WINDOW_SECONDS", "900")
    ip = "198.51.100.12"
    clear_guest_limit(ip)
    monkeypatch.setattr("app.main.run_investigation", lambda *args: {"completed": True})
    first = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    second = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers.get("retry-after")