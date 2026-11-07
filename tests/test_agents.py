from pathlib import Path
import csv
import json
import sys
import types as pytypes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import run_investigation


def read_sample(name):
    path = Path(__file__).resolve().parents[1] / "data" / "sample" / name
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_orchestration_runs_four_specialists_then_planner_and_auditor(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    calls = []
    attempts = {}

    def fake_generate_content(model, contents, config):
        name = contents.split("You are the ", 1)[1].split(" in SupplyPilot", 1)[0]
        calls.append(name)
        assert config["response_mime_type"] == "application/json"
        assert config["response_json_schema"]["type"] == "object"
        attempts[name] = attempts.get(name, 0) + 1
        if name == "Inventory Risk Agent" and attempts[name] == 1:
            # Reproduce the malformed/truncated JSON returned by the live model.
            return pytypes.SimpleNamespace(text='{"summary":"Inventory risk')
        if name == "Demand Forecasting Agent":
            output = {"summary":"Recent demand was reviewed.","key_findings":["Forecast output reviewed."],"risks":[],"forecast_confidence":"medium","watchlist_skus":["SKU-101"]}
        elif name == "Inventory Risk Agent":
            output = {"summary":"Inventory cover was reviewed.","key_findings":["Stock exposure reviewed."],"risks":[],"critical_skus":[],"assumptions":[]}
        elif name == "Supplier Intelligence Agent":
            output = {"summary":"Supplier alternatives were compared.","key_findings":["Supplier metrics compared."],"risks":[],"preferred_suppliers":[],"caveats":[]}
        elif name == "Supply Risk Agent":
            output = {"summary":"Supply resilience was reviewed.","key_findings":["Supplier concentration reviewed."],"risks":[],"single_source_skus":[],"mitigations":[]}
        elif name == "Procurement Planning Agent":
            output = {"decision_summary":"Prioritize the SKUs with shorter cover.","procurement_rationale":["The proposal respects recorded supplier constraints."],"priority_actions":["Review proposed orders."],"trade_offs":[],"assumptions":[]}
        elif name == "Decision Auditor Agent":
            assert config["response_json_schema"]["properties"]["approved_for_human_review"]["type"] == "boolean"
            output = {"approved_for_human_review":True,"concerns":[],"checks":[{"check":"Evidence is present","result":"pass","evidence":"Submitted input datasets."}],"required_human_actions":["Verify supplier terms before purchase."]}
        else:
            raise AssertionError(f"Unexpected agent: {name}")
        return pytypes.SimpleNamespace(text=json.dumps(output))

    fake_models = pytypes.SimpleNamespace(generate_content=fake_generate_content)
    fake_client = pytypes.SimpleNamespace(models=fake_models)
    fake_genai = pytypes.ModuleType("google.genai")
    fake_genai.Client = lambda api_key: fake_client
    fake_types = pytypes.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    fake_genai.types = fake_types
    fake_google = pytypes.ModuleType("google")
    fake_google.__path__ = []
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

    progress = []
    result = run_investigation(
        "Test replenishment planning workflow",
        read_sample("sales_history.csv"),
        read_sample("inventory.csv"),
        read_sample("suppliers.csv"),
        progress_callback=lambda name, status: progress.append((name, status)),
    )

    assert len(calls) == 7  # one malformed specialist response was retried once
    assert attempts["Inventory Risk Agent"] == 2
    assert set(calls) == {
        "Demand Forecasting Agent", "Inventory Risk Agent", "Supplier Intelligence Agent",
        "Supply Risk Agent", "Procurement Planning Agent", "Decision Auditor Agent",
    }
    assert result["metrics"]["gemini_calls"] == 7
    assert result["metrics"]["tool_calls"] == 6
    assert result["status"] == "ready_for_human_review"
    assert result["data"]["hard_constraint_issues"] == []
    completed_names = {name for name, status in progress if status == "completed"}
    started_names = {name for name, status in progress if status == "running"}
    assert completed_names == started_names == set(calls)


def test_daily_free_tier_quota_is_not_retried_and_returns_reset_guidance(monkeypatch):
    import pytest
    from app.agents import AgentRuntimeError, _is_daily_quota_error, _is_rate_limit_error, _retry_delay_seconds

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    calls = []
    quota_message = (
        "429 RESOURCE_EXHAUSTED: Generate content requests per day limit exceeded "
        "on Free Tier. retryDelay': '2650s'"
    )
    assert _is_daily_quota_error(RuntimeError(quota_message))
    assert not _is_rate_limit_error(RuntimeError(quota_message))
    assert _retry_delay_seconds(RuntimeError(quota_message)) == 2650

    def fail_quota(model, contents, config):
        calls.append(contents.split("You are the ", 1)[1].split(" in SupplyPilot", 1)[0])
        raise RuntimeError(quota_message)

    fake_models = pytypes.SimpleNamespace(generate_content=fail_quota)
    fake_client = pytypes.SimpleNamespace(models=fake_models)
    fake_genai = pytypes.ModuleType("google.genai")
    fake_genai.Client = lambda api_key: fake_client
    fake_types = pytypes.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    fake_genai.types = fake_types
    fake_google = pytypes.ModuleType("google")
    fake_google.__path__ = []
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

    with pytest.raises(AgentRuntimeError) as error:
        run_investigation(
            "Test daily quota guard",
            read_sample("sales_history.csv"),
            read_sample("inventory.csv"),
            read_sample("suppliers.csv"),
        )

    assert len(calls) <= 4  # one initial request per parallel specialist at most
    assert error.value.quota_reset_seconds == 2650
    assert "daily request quota" in str(error.value).lower()
    assert "stopped further gemini retries" in str(error.value).lower()