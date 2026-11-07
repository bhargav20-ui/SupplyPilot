from pathlib import Path
import sys
import time
import threading

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


def wait_for_task(task_id, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/investigations/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"completed", "failed"}:
            return body
        time.sleep(0.01)
    raise AssertionError("Background investigation did not finish within the test timeout.")


def test_health_endpoint_reports_public_runtime_without_exposing_secret(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEY", "legacy-private-key")
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.2.1"
    assert body["public_access"] is True
    assert body["rate_limit"]["runs"] >= 1
    assert "legacy-private-key" not in str(body)
    assert "GEMINI_API_KEY" not in str(body)


def test_sample_endpoint_returns_three_datasets():
    response = client.get("/api/sample")
    assert response.status_code == 200
    body = response.json()
    assert len(body["sales"]) == 168
    assert len(body["inventory"]) == 4
    assert len(body["suppliers"]) == 11
    assert {"date", "sku", "product", "units_sold"}.issubset(body["sales"][0])
    assert {"sku", "product", "on_hand", "lead_time_days", "safety_stock"}.issubset(body["inventory"][0])
    assert {"supplier", "sku", "unit_cost", "lead_time_days", "on_time_rate", "quality_rate", "capacity_units", "min_order_qty"}.issubset(body["suppliers"][0])


def test_root_serves_public_dashboard_without_workspace_key_field():
    response = client.get("/")
    assert response.status_code == 200
    assert "SupplyPilot" in response.text
    assert "Run analysis" in response.text
    assert "workspace-key" not in response.text


def test_investigation_returns_task_id_and_missing_key_fails_as_json(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ip = "198.51.100.10"
    clear_guest_limit(ip)
    started = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    assert started.status_code == 202
    task_id = started.json()["task_id"]
    result = wait_for_task(task_id)
    assert result["status"] == "failed"
    assert "GEMINI_API_KEY is not configured" in result["error"]
    assert result["metrics"] == {"gemini_calls": 0, "tool_calls": 0}


def test_public_guest_can_run_without_shared_access_key(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEY", "legacy-private-key")
    monkeypatch.setenv("PUBLIC_RUN_LIMIT", "5")
    ip = "198.51.100.11"
    clear_guest_limit(ip)

    def fake_run(*args, progress_callback=None):
        if progress_callback:
            progress_callback("Demand Forecasting Agent", "running")
            progress_callback("Demand Forecasting Agent", "completed")
        return {"completed": True, "status": "test", "metrics": {"gemini_calls": 6, "tool_calls": 6}}

    monkeypatch.setattr("app.main.run_investigation", fake_run)
    started = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    assert started.status_code == 202
    result = wait_for_task(started.json()["task_id"])
    assert result["status"] == "completed"
    assert result["result"]["completed"] is True
    assert result["agents"]["Demand Forecasting Agent"] == "completed"


def test_public_guest_rate_limit_rejects_excess_runs(monkeypatch):
    monkeypatch.setenv("PUBLIC_RUN_LIMIT", "1")
    monkeypatch.setenv("PUBLIC_RUN_WINDOW_SECONDS", "900")
    ip = "198.51.100.12"
    clear_guest_limit(ip)
    gate = threading.Event()

    def slow_run(*args, progress_callback=None):
        gate.wait(0.25)
        return {"completed": True, "metrics": {"gemini_calls": 6, "tool_calls": 6}}

    monkeypatch.setattr("app.main.run_investigation", slow_run)
    first = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    second = client.post("/api/investigate", headers={"x-forwarded-for": ip}, json=sample_payload())
    gate.set()
    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers.get("retry-after")
    wait_for_task(first.json()["task_id"])


def test_unknown_task_returns_json_404():
    response = client.get("/api/investigations/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_sample_reader_skips_sheet_preamble_and_export_index(tmp_path, monkeypatch):
    from app import main as main_module
    (tmp_path / "data" / "sample").mkdir(parents=True)
    sample = tmp_path / "data" / "sample" / "sales_history.csv"
    sample.write_text(
        "Sheet export\nDataset: Sales\nindex,date,sku,product,units_sold\n"
        "0,2026-01-01,A,Alpha,12\n1,2026-01-02,A,Alpha,14\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main_module, "BASE_DIR", tmp_path)
    rows = main_module._read_sample("sales_history.csv")
    assert len(rows) == 2
    assert rows[0] == {"date": "2026-01-01", "sku": "A", "product": "Alpha", "units_sold": "12"}


def test_daily_quota_circuit_breaker_blocks_new_public_jobs(monkeypatch):
    import time
    from app import main as main_module

    monkeypatch.setattr(main_module, "_GEMINI_QUOTA_PAUSED_UNTIL", time.time() + 120)
    ip = "198.51.100.90"
    clear_guest_limit(ip)
    response = client.post(
        "/api/investigate",
        headers={"x-forwarded-for": ip},
        json=sample_payload(),
    )
    assert response.status_code == 429
    assert "daily request quota" in response.json()["detail"].lower()
    assert int(response.headers["retry-after"]) > 0


def test_health_reports_gemini_quota_circuit_breaker_state(monkeypatch):
    import time
    from app import main as main_module

    monkeypatch.setattr(main_module, "_GEMINI_QUOTA_PAUSED_UNTIL", time.time() + 120)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["gemini_quota_paused"] is True
    assert response.json()["gemini_quota_retry_after_seconds"] > 0