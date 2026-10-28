"""Third path regression tests. No external job lookup, OpenAI call, or PDF conversion."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import pasted_job_descriptions as pasted


JD = (
    "Role: Data Engineer on GCP with GenAI\n"
    "Required: five years of Data Engineering and SQL data modeling, three years "
    "of Python, four years of Google Cloud Platform experience and three years "
    "of GenAI experience, preferably Vertex AI. Build trusted data pipelines."
)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    root = tmp_path / "generated" / "pasted_job_descriptions"
    monkeypatch.setattr(pasted, "JD_DIR", root)
    monkeypatch.setattr(pasted, "STATE_FILE", root / "jobs.json")
    monkeypatch.setattr(pasted, "WORK_DIR", root / "work")
    monkeypatch.setattr(pasted, "ARTIFACT_DIR", root / "resumes")
    return root


def test_pasted_path_is_distinct_and_persistent(isolated):
    created = pasted.add_description(pasted.DescriptionInput(description=JD, company="Acme"))
    assert created["status"] == "QUEUED"
    assert created["title"] == "Data Engineer on GCP with GenAI"
    assert created["company"] == "Acme"
    assert created["source"] == "pasted_jd"
    assert created["posting_verified"] is False
    assert created["resume_available"] is False
    assert "description" not in created
    assert pasted.list_descriptions()["counts"]["total"] == 1
    duplicate = pasted.add_description(pasted.DescriptionInput(description=JD, company="Acme"))
    assert duplicate["key"] == created["key"]
    assert pasted.list_descriptions()["counts"]["total"] == 1


def test_generation_reuses_production_pipeline_and_keeps_jobs_unverified(isolated, monkeypatch):
    values = {}

    def fake_shared(raw, *, artifact_dir, work_dir):
        values.update(raw)
        assert artifact_dir == pasted.ARTIFACT_DIR
        assert work_dir == pasted.WORK_DIR
        target = artifact_dir / pasted.manual_job_links._safe_key(raw["external_id"]) / "run1"
        target.mkdir(parents=True)
        docx = target / "tailored.docx"
        pdf = target / "tailored.pdf"
        docx.write_bytes(b"word-test")
        pdf.write_bytes(b"pdf-test")
        return {
            "next_action": "READY_TO_APPLY", "resume_path": str(docx),
            "pdf_path": str(pdf), "ats_audit": {"passed": True},
            "artifact_validation": {"passed": True},
            "resume_tailoring_policy": {"mode": "JD_DRIVEN_LIGHT"},
        }

    monkeypatch.setattr(pasted.manual_job_links, "run_shared_resume_pipeline", fake_shared)
    row = pasted.add_description(pasted.DescriptionInput(description=JD))
    result = pasted.process_description(row["key"])
    assert result["status"] == "RESUME_READY"
    assert result["next_action"] == "READY_TO_APPLY"
    assert result["resume_available"] and result["docx_available"]
    assert "description" not in result
    assert values["application_route"] == "PASTED_JD"
    assert values["description_complete"] is True
    assert values["tailoring_mode"] == "FULL_JD"
    assert values["posting_verified"] is False
    assert values["url"] is None
    assert "aws" not in values["description"].lower()
    assert pasted.list_descriptions()["counts"]["ready"] == 1
    with pytest.raises(HTTPException) as exc:
        pasted.api_confirm(row["key"])
    assert exc.value.status_code == 409
    pasted.api_delete(row["key"])
    assert not pasted.ARTIFACT_DIR.exists() or not any(pasted.ARTIFACT_DIR.rglob("*"))
    assert pasted.list_descriptions()["counts"]["total"] == 0


def test_failure_stays_visible_and_retryable(isolated, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("OpenAI API unavailable")

    monkeypatch.setattr(pasted.manual_job_links, "run_shared_resume_pipeline", fail)
    row = pasted.add_description(pasted.DescriptionInput(description=JD, title="GCP Engineer"))
    got = pasted.process_description(row["key"])
    assert got["status"] == "HOLD_RESUME_ERROR"
    assert "unavailable" in got["error"]
    assert pasted.list_descriptions()["counts"]["attention"] == 1


def test_access_token_required_and_compared_safely(monkeypatch):
    credentials = SimpleNamespace(username="admin", password="correct")
    monkeypatch.delenv("PASTED_JD_ACCESS_TOKEN", raising=False)
    with pytest.raises(HTTPException) as missing:
        pasted._authenticate(credentials)
    assert missing.value.status_code == 503
    monkeypatch.setenv("PASTED_JD_ACCESS_TOKEN", "correct")
    assert pasted._authenticate(credentials) is None
    with pytest.raises(HTTPException) as wrong:
        pasted._authenticate(SimpleNamespace(username="admin", password="incorrect"))
    assert wrong.value.status_code == 401


def test_dashboard_exposes_third_path():
    from app.dashboard import app
    paths = {route.path for route in app.routes}
    assert {"/pasted-jds", "/api/pasted-jds", "/pasted-jd-resume/{key}/{kind}"} <= paths


def test_malicious_local_application_url_is_rejected(isolated):
    with pytest.raises(ValueError):
        pasted.add_description(pasted.DescriptionInput(description=JD, url="http://127.0.0.1/private"))
