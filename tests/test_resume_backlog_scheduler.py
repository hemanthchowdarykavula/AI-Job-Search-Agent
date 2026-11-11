"""Production reliability: bounded cycles must not silently discard verified jobs."""
import json
import os
import time

from app import batch_prepare, production_cycle, scheduled_runner
from app.job_ledger import load_ledger, retryable_jobs


def test_verified_job_survives_renderer_outage_and_time_budget(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    verified = {
        "action": "FINAL_JD_VERIFIED",
        "eligibility": {"experience": "PASS", "sponsorship": "PASS"},
        "job": {
            "external_id": "deferred-001",
            "source": "greenhouse",
            "company_key": "Example Employer",
            "title": "Data Engineer",
            "url": "https://example.test/jobs/de-001",
            "description": "Python SQL AWS ETL pipeline engineering",
            "description_complete": True,
            "freshness_proof": {
                "posted_at": "2026-11-11T14:00:00Z",
                "production_cutoff": "2026-11-11T12:30:00Z",
            },
            "live_check": {"passed": True},
        },
    }
    production_cycle._sync_finalized([verified], str(ledger_path), "cycle-test")
    rows = retryable_jobs(load_ledger(str(ledger_path)))
    assert len(rows) == 1
    assert rows[0]["external_id"] == "deferred-001"
    assert rows[0]["eligibility"] == verified["eligibility"]
    assert rows[0]["freshness_proof"] == verified["job"]["freshness_proof"]


def test_batch_prepare_returns_gracefully_after_budget_expires(tmp_path, monkeypatch):
    report = tmp_path / "verified.json"
    manifest = tmp_path / "manifest.json"
    report.write_text(json.dumps({"results": [
        {"action": "FINAL_JD_VERIFIED", "job": {"external_id": "later-001"}}
    ]}), encoding="utf-8")
    monkeypatch.setenv("JOB_CYCLE_DEADLINE_EPOCH", str(time.time() - 10))
    result = batch_prepare.prepare(str(report), str(manifest))
    assert result == []
    assert json.loads(manifest.read_text(encoding="utf-8")) == []


def test_cycle_budget_environment_is_restored(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduled_runner, "ROOT", tmp_path)
    monkeypatch.setattr(scheduled_runner, "STATE_PATH", tmp_path / "scheduler_state.json")
    (tmp_path / "sources.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PRODUCTION_CYCLE_BUDGET_SECONDS", "6000")
    monkeypatch.delenv("JOB_CYCLE_DEADLINE_EPOCH", raising=False)
    seen = []

    def fake_cycle(**kwargs):
        seen.append(float(os.environ["JOB_CYCLE_DEADLINE_EPOCH"]))
        return {"cycle_id": "fake", "source_status": {}, "source_errors": {}}

    monkeypatch.setattr(scheduled_runner, "run_cycle", fake_cycle)
    result = scheduled_runner.run_scheduled("sources.json", generate_resumes=False, force=True)
    assert result["cycle_id"] == "fake"
    assert seen and seen[0] > time.time()
    assert "JOB_CYCLE_DEADLINE_EPOCH" not in os.environ
