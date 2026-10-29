import json
from datetime import datetime, timezone

import pytest

from app import jd_finalizer


def _eligible_job(url, source="career_site"):
    return {
        "external_id": "agg:1",
        "source": source,
        "company_key": "Example Health",
        "title": "Data Engineer",
        "location": "United States",
        "employment_type": "Full-Time",
        "url": url,
        "original_url": url,
        "description": "Responsibilities " + ("build data pipelines with Python and SQL. " * 80),
        "description_complete": True,
    }


def _allow_finalization(monkeypatch, official_page=""):
    monkeypatch.setattr(jd_finalizer, "load_profile", lambda: {})
    monkeypatch.setattr(jd_finalizer, "resolve_full_jd", lambda j: j)
    monkeypatch.setattr(jd_finalizer, "_fetch_public_page", lambda url: official_page)
    monkeypatch.setattr(jd_finalizer, "_live_public_job_page", lambda url: (True, "reachable"))
    monkeypatch.setattr(jd_finalizer, "two_category_filter", lambda j, p: {"eligible": True})
    monkeypatch.setattr(jd_finalizer, "passes_hard_filters", lambda j, p: (True, []))


def test_linkedin_job_cannot_reach_resume_without_employer_ats(monkeypatch, tmp_path):
    report = tmp_path / "eligible.json"
    output = tmp_path / "finalized.json"
    job = _eligible_job("https://www.linkedin.com/jobs/view/123456")
    report.write_text(json.dumps({"results": [{"action": "ELIGIBLE_FOR_RESUME", "job": job}]}), encoding="utf-8")
    monkeypatch.setattr(jd_finalizer, "load_profile", lambda: {})
    monkeypatch.setattr(jd_finalizer, "resolve_full_jd", lambda j: j)
    monkeypatch.setattr(jd_finalizer, "_live_public_job_page", lambda url: (_ for _ in ()).throw(AssertionError("aggregator URL must be held before live validation")))
    result = jd_finalizer.finalize_report(str(report), str(output))
    assert result["finalized"] == 0
    assert result["held_or_rejected"] == 1
    assert result["rejections"][0]["action"] == "HOLD_ATS_UNRESOLVED"


def test_resolved_aggregator_job_can_use_verified_external_ats(monkeypatch, tmp_path):
    report = tmp_path / "eligible.json"
    output = tmp_path / "finalized.json"
    job = _eligible_job("https://boards.greenhouse.io/example/jobs/123456")
    job.update({
        "aggregator_url": "https://www.linkedin.com/jobs/view/123456",
        "ats_provider": "greenhouse",
        "ats_resolution": "employer_career_page_canonical",
    })
    report.write_text(json.dumps({"results": [{"action": "ELIGIBLE_FOR_RESUME", "job": job}]}), encoding="utf-8")
    _allow_finalization(monkeypatch, '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-09-27T12:00:00+00:00"}</script>')
    result = jd_finalizer.finalize_report(str(report), str(output), now=datetime(2026,9,28,11,0,tzinfo=timezone.utc))
    assert result["finalized"] == 1
    assert result["results"][0]["job"]["application_route"] == "EXTERNAL_ATS"


@pytest.mark.parametrize("source", ["dice", "ziprecruiter", "monster", "indeed", "linkedin"])
def test_resolved_job_board_cannot_use_board_date_without_official_date(monkeypatch, tmp_path, source):
    report = tmp_path / f"eligible-{source}.json"
    output = tmp_path / f"finalized-{source}.json"
    job = _eligible_job("https://boards.greenhouse.io/example/jobs/123456", source=source)
    job.update({
        "aggregator_url": f"https://www.{source}.com/jobs/123456",
        "ats_provider": "greenhouse",
        "ats_resolution": "employer_career_page_canonical",
        "posted_on": "2026-10-05T12:00:00+00:00",
    })
    report.write_text(json.dumps({"results": [{"action": "ELIGIBLE_FOR_RESUME", "job": job}]}), encoding="utf-8")
    _allow_finalization(monkeypatch, "<html><body>No posting date is shown.</body></html>")
    result = jd_finalizer.finalize_report(
        str(report), str(output), hours=3, now=datetime(2026,10,5,13,0,tzinfo=timezone.utc)
    )
    assert result["finalized"] == 0
    assert result["rejections"][0]["action"] == "HOLD_POST_DATE_UNVERIFIED"
    assert "employer/ats posting date" in result["rejections"][0]["reason"].lower()


def test_official_date_still_overrides_newer_job_board_date(monkeypatch, tmp_path):
    report = tmp_path / "eligible.json"
    output = tmp_path / "finalized.json"
    job = _eligible_job("https://boards.greenhouse.io/example/jobs/123456", source="dice")
    job.update({
        "aggregator_url": "https://www.dice.com/job-detail/123456",
        "ats_provider": "greenhouse",
        "ats_resolution": "employer_career_page_canonical",
        "posted_on": "2026-10-05T12:00:00+00:00",
    })
    report.write_text(json.dumps({"results": [{"action": "ELIGIBLE_FOR_RESUME", "job": job}]}), encoding="utf-8")
    _allow_finalization(monkeypatch, '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-10-01T12:00:00+00:00"}</script>')
    result = jd_finalizer.finalize_report(
        str(report), str(output), hours=3, now=datetime(2026,10,5,13,0,tzinfo=timezone.utc)
    )
    assert result["finalized"] == 0
    assert result["rejections"][0]["action"] == "REJECT_STALE_OFFICIAL_POSTING"
    assert result["rejections"][0]["job"]["freshness_basis"] == "official_employer_posting_date"


def test_missing_official_and_job_board_date_is_held(monkeypatch, tmp_path):
    report = tmp_path / "eligible.json"
    output = tmp_path / "finalized.json"
    job = _eligible_job("https://boards.greenhouse.io/example/jobs/123456", source="monster")
    job.update({
        "aggregator_url": "https://www.monster.com/job-openings/123456",
        "ats_provider": "greenhouse",
        "ats_resolution": "employer_career_page_canonical",
    })
    report.write_text(json.dumps({"results": [{"action": "ELIGIBLE_FOR_RESUME", "job": job}]}), encoding="utf-8")
    _allow_finalization(monkeypatch, "<html><body>No posting date is shown.</body></html>")
    result = jd_finalizer.finalize_report(
        str(report), str(output), hours=3, now=datetime(2026,10,5,13,0,tzinfo=timezone.utc)
    )
    assert result["finalized"] == 0
    assert result["rejections"][0]["action"] == "HOLD_POST_DATE_UNVERIFIED"
