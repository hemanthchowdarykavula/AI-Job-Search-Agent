"""Five user-approved boards can publish manual-apply resumes without an ATS match."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app import jd_finalizer, application_queue, production_acceptance
from app.approved_job_boards import (
    approved_board, board_url_is_valid, board_application_url, board_date,
    BOARD_APPLICATION_ROUTE,
)
from app.sources import himalayas

LISTINGS = {
    "hiringcafe": "https://hiringcafe.com/job/data-engineer-example-abc123",
    "welcome_to_the_jungle": "https://www.welcometothejungle.com/en/companies/acme/jobs/data-engineer",
    "himalayas": "https://himalayas.app/companies/acme/jobs/data-engineer",
    "yc_jobs": "https://www.ycombinator.com/companies/acme/jobs/data-engineer",
    "wellfound": "https://wellfound.com/jobs/123456-data-engineer",
}
FULL_DESC = ("Responsibilities: Build Python SQL PySpark ETL pipelines for an analytics warehouse. "
             "Requirements: 3 years of Python, SQL and database data engineering experience. "
             "Qualifications: Strong data modeling, ETL and cloud platform expertise. ") * 9
NOW = datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc)
POSTED = "2026-10-29T12:00:00+00:00"


def configure(monkeypatch):
    monkeypatch.setattr(jd_finalizer, "load_profile", lambda: {})
    monkeypatch.setattr(jd_finalizer, "two_category_filter", lambda job, profile: {"eligible": True})
    monkeypatch.setattr(jd_finalizer, "passes_hard_filters", lambda job, profile: (True, []))
    monkeypatch.setattr(jd_finalizer, "_live_public_job_page", lambda url: (True, "source_page_alive"))
    monkeypatch.setattr(jd_finalizer, "_fetch_public_page", lambda url: "")
    monkeypatch.setattr(jd_finalizer, "himalayas_open_details",
                        lambda job: (True, "himalayas_api_open", {"guid": LISTINGS["himalayas"]}))


def run_one(tmp_path, source, *, date=POSTED, url=None, description=FULL_DESC, extra=None):
    url = LISTINGS[source] if url is None else url
    raw = {
        "external_id": source+":123", "source": source, "company_key": "Acme",
        "title": "Data Engineer", "location": "United States",
        "employment_type": "Full Time", "description": description,
        "url": url, "original_url": url,
        "posted_on": date, "board_listing_verified": True,
        "discovery_only": True, "authoritative_source": False,
    }
    raw.update(extra or {})
    inp=tmp_path/"eligible.json"
    out=tmp_path/"finalized.json"
    inp.write_text(json.dumps({"results": [{"action": "ELIGIBLE_FOR_RESUME", "job": raw}]}))
    return jd_finalizer.finalize_report(str(inp), str(out), hours=24, now=NOW,
                                         since="2026-10-28T13:00:00+00:00")


@pytest.mark.parametrize("source", list(LISTINGS))
def test_five_board_jobs_reach_final_resume_gate_without_employer_ats(tmp_path,monkeypatch,source):
    configure(monkeypatch)
    result=run_one(tmp_path,source)
    assert result["finalized"] == 1, result["rejections"]
    item=result["results"][0]["job"]
    assert item["application_route"] == BOARD_APPLICATION_ROUTE
    assert item["posting_verification_scope"] == "JOB_BOARD_ONLY"
    assert item["source_apply_url"] == LISTINGS[source]
    assert item["original_url"] == LISTINGS[source]
    assert item.get("ats_provider") is None
    assert item["freshness_proof"]["posted_at"] == POSTED
    assert item["freshness_basis"] in {"job_board_posting_date","job_board_last_updated"}
    assert item.get("official_posted_at") is None
    assert item["live_check"]["passed"] is True


def test_board_record_missing_date_is_held_instead_of_fabricated(tmp_path,monkeypatch):
    configure(monkeypatch)
    result=run_one(tmp_path,"wellfound",date=None)
    assert result["finalized"]==0
    assert result["rejections"][0]["action"]=="HOLD_BOARD_DATE_UNVERIFIED"


def test_board_record_with_wrong_domain_is_held(tmp_path,monkeypatch):
    configure(monkeypatch)
    result=run_one(tmp_path,"himalayas",url="https://evil.example/job/123")
    assert result["finalized"]==0
    assert result["rejections"][0]["action"]=="HOLD_BOARD_URL_UNVERIFIED"


def test_job_board_application_links_are_specific_and_https():
    for source,url in LISTINGS.items():
        assert approved_board(source)
        assert board_url_is_valid(source,url)
        assert board_application_url({"source":source,"url":url}) == url
        assert not board_url_is_valid(source,"https://example.com/jobs/123")
        assert not board_url_is_valid(source,url.replace("https://","http://"))
        assert not board_url_is_valid(source,url.split("/")[0]+"//"+url.split("/")[2]+"/")


def test_board_source_feed_proof_allows_login_restricted_manual_link(tmp_path,monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(jd_finalizer,"_live_public_job_page",lambda url: (None,"http_403"))
    result=run_one(tmp_path,"wellfound")
    job=result["results"][0]["job"]
    assert result["finalized"]==1
    assert job["live_check"]["browser_access_unverified"] is True
    assert job["posting_verification_scope"]=="JOB_BOARD_ONLY"


def test_other_aggregators_still_require_official_ats(tmp_path,monkeypatch):
    configure(monkeypatch)
    source="dice"
    u="https://www.dice.com/job-detail/123"
    raw={"external_id":"dice:123","source":source,"company_key":"Acme",
         "title":"Data Engineer","description":FULL_DESC,"url":u,
         "original_url":u,"posted_on":POSTED}
    monkeypatch.setattr(jd_finalizer, "resolve_full_jd", lambda j: j)
    inp=tmp_path/"eligible.json"
    out=tmp_path/"finalized.json"
    inp.write_text(json.dumps({"results":[{"action":"ELIGIBLE_FOR_RESUME","job":raw}]}))
    result=jd_finalizer.finalize_report(str(inp),str(out),hours=24,now=NOW)
    assert result["finalized"]==0
    assert result["rejections"][0]["action"]=="HOLD_ATS_UNRESOLVED"


def test_himalayas_country_objects_are_allowed():
    row={"title":"Data Engineer","guid":LISTINGS["himalayas"],"applicationLink":LISTINGS["himalayas"],
         "companyName":"Acme","locationRestrictions":[{"alpha2":"US","name":"United States"}],
         "description":FULL_DESC, "pubDate":1793275200}
    item=himalayas.normalize(row)
    assert item and item["source"]=="himalayas"
    assert item["board_listing_verified"] is True
    assert item["location"]=="Remote, United States"
    assert item["source_apply_url"]==LISTINGS["himalayas"]
    assert himalayas.normalize({**row,"locationRestrictions":[{"alpha2":"GB"}]}) is None


def test_queue_and_production_acceptance_preserve_manual_board_url(tmp_path,monkeypatch):
    source="wellfound"
    url=LISTINGS[source]
    pdf=tmp_path/"tailored.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    row={
        "external_id":"wellfound:123","source":source,"company":"Acme","title":"Data Engineer",
        "location":"United States","employment_type":"Full Time",
        "description":FULL_DESC,"next_action":"READY_TO_APPLY","pdf_path":str(pdf),
        "application_route":BOARD_APPLICATION_ROUTE, "posting_verification_scope":"JOB_BOARD_ONLY",
        "board_url":url, "url":url, "original_url":url,
        "source_credit":"Wellfound",
        "freshness_basis":"job_board_posting_date", "board_posted_at":POSTED,
        "freshness_proof":{"posted_at":POSTED,"basis":"job_board_posting_date",
                           "production_cutoff":"2026-10-28T13:00:00+00:00",
                           "checked_at":NOW.isoformat()},
        "live_check":{"passed":True,"verification_scope":"JOB_BOARD_ONLY"},
        "artifact_validation":{"passed":True},
    }
    manifest=tmp_path/"manifest.json";queue_file=tmp_path/"queue.json"
    manifest.write_text(json.dumps([row]))
    monkeypatch.setattr(application_queue,"load_profile",lambda:{})
    monkeypatch.setattr(application_queue,"passes_hard_filters",lambda *args: (True,[]))
    queue=application_queue.build(str(manifest),str(queue_file))
    assert len(queue)==1
    assert queue[0]["url"]==url
    assert queue[0]["application_route"]==BOARD_APPLICATION_ROUTE
    assert queue[0]["posting_verification_scope"]=="JOB_BOARD_ONLY"
    monkeypatch.setattr(production_acceptance,"load_profile",lambda:{})
    monkeypatch.setattr(production_acceptance,"passes_hard_filters",lambda *args:(True,[]))
    summary_file=tmp_path/"summary.json"
    summary_file.write_text(json.dumps({
        "cycle_id":"board-e2e", "ready_to_apply":1,"queued_for_application":1,
        "eligible":1,"manifest_ready_to_apply":1,"prepared":1,
        "resume_generation_enabled":True,"application_queue":str(queue_file),
        "production_cutoff":"2026-10-28T13:00:00+00:00"
    }))
    result=production_acceptance.audit(str(summary_file))
    assert result["passed"] is True


def test_hiringcafe_enriches_short_card_with_official_full_job_detail(tmp_path, monkeypatch):
    configure(monkeypatch)
    seen=[]
    monkeypatch.setattr(jd_finalizer,"hiringcafe_full_description",
                        lambda job_id: seen.append(job_id) or FULL_DESC)
    result=run_one(tmp_path,"hiringcafe",description="Data engineering opportunity",
                   extra={"provider_job_id":"abcdefghijklmnop"})
    assert result["finalized"]==1, result["rejections"]
    job=result["results"][0]["job"]
    assert seen == ["abcdefghijklmnop"]
    assert job["jd_resolution_source"]=="hiringcafe_official_get_job_details"
    assert job["description_usable"] and job["description_complete"]
    assert job["application_route"] == BOARD_APPLICATION_ROUTE


def test_mcp_preserves_official_markdown_full_description():
    from types import SimpleNamespace
    from app.sources.mcp_jobs import _content_to_payload
    response=SimpleNamespace(structured_content=None,meta={},
                             content=[SimpleNamespace(text="# Data Engineer\n\n"+FULL_DESC)])
    got=_content_to_payload(response)
    assert FULL_DESC in got["_mcp_text"]
