import json
from app import jd_finalizer


def test_verified_long_tail_ats_is_finalized_for_external_handoff(monkeypatch,tmp_path):
    report=tmp_path/"eligible.json"
    output=tmp_path/"finalized.json"
    job={
        "external_id":"manatal:1","source":"manatal","company_key":"Example Staffing",
        "title":"Data Engineer","location":"United States","employment_type":"Full-Time",
        "url":"https://example.careers-page.com/jobs/1","original_url":"https://example.careers-page.com/jobs/1",
        "ats_provider":"manatal","description":"Responsibilities " + ("build data pipelines Python SQL. "*80),
        "description_complete":True,
    }
    report.write_text(json.dumps({"results":[{"action":"ELIGIBLE_FOR_RESUME","job":job}]}),encoding="utf-8")
    monkeypatch.setattr(jd_finalizer,"load_profile",lambda:{})
    monkeypatch.setattr(jd_finalizer,"resolve_full_jd",lambda j:j)
    monkeypatch.setattr(jd_finalizer,"_live_public_job_page",lambda url:(True,"reachable"))
    monkeypatch.setattr(jd_finalizer,"two_category_filter",lambda j,p:{"eligible":True})
    monkeypatch.setattr(jd_finalizer,"passes_hard_filters",lambda j,p:(True,[]))
    result=jd_finalizer.finalize_report(str(report),str(output))
    assert result["finalized"]==1
    raw=result["results"][0]["job"]
    assert raw["application_route"]=="EXTERNAL_ATS"
    assert "manual_application_required" not in raw


def test_dice_board_date_cannot_replace_missing_employer_date(monkeypatch,tmp_path):
    from datetime import datetime, timezone
    report=tmp_path/"eligible_dice.json"; output=tmp_path/"finalized_dice.json"
    job={
        "external_id":"dice:1","source":"dice","company_key":"Example Co",
        "title":"Data Engineer","location":"United States","employment_type":"Full-Time",
        "url":"https://www.dice.com/job-detail/1","original_url":"https://jobs.example.com/data-engineer",
        "aggregator_url":"https://www.dice.com/job-detail/1",
        "ats_resolution":"employer_career_page_canonical","ats_provider":"greenhouse",
        "posted_at":"2026-09-28T15:00:00+00:00",
        "description":"Responsibilities " + ("build data pipelines Python SQL Spark. "*80),
        "description_complete":True,
    }
    report.write_text(json.dumps({"results":[{"action":"ELIGIBLE_FOR_RESUME","job":job}]}),encoding="utf-8")
    monkeypatch.setattr(jd_finalizer,"load_profile",lambda:{})
    monkeypatch.setattr(jd_finalizer,"resolve_full_jd",lambda j:j)
    monkeypatch.setattr(jd_finalizer,"_fetch_public_page",lambda url:"<html><body>Job open, no posted date</body></html>")
    monkeypatch.setattr(jd_finalizer,"_live_public_job_page",lambda url:(True,"reachable"))
    monkeypatch.setattr(jd_finalizer,"two_category_filter",lambda j,p:{"eligible":True})
    monkeypatch.setattr(jd_finalizer,"passes_hard_filters",lambda j,p:(True,[]))
    result=jd_finalizer.finalize_report(str(report),str(output),hours=24,now=datetime(2026,9,28,17,0,tzinfo=timezone.utc))
    assert result["finalized"]==0
    assert result["rejections"][0]["action"]=="HOLD_POST_DATE_UNVERIFIED"
    assert result["rejections"][0]["job"]["discovery_posted_at"]=="2026-09-28T15:00:00+00:00"


def test_resolve_full_jd_uses_same_workday_authoritative_metadata_for_every_path(monkeypatch):
    url = "https://eaton.wd5.myworkdayjobs.com/en-US/Eaton_Careers/job/Beachwood-OH/Lead-AI-and-Data-Engineer_JR12345"
    jd = (
        "Responsibilities: design and deploy scalable AI and data engineering solutions using Azure OpenAI, Python, SQL, and Databricks. "
        "Requirements: strong software engineering, data engineering, machine learning, cloud architecture, CI/CD, and production support experience. "
        "Qualifications: bachelor's or master's degree and relevant engineering experience. "
    ) * 8
    detail = {
        "title": "Lead AI and Data Engineer",
        "jobDescription": jd,
        "location": "Beachwood, OH",
        "additionalLocations": ["Mountainside, NJ"],
        "timeType": "Full time",
        "jobReqId": "JR12345",
    }
    monkeypatch.setattr(jd_finalizer, "resolve_original_ats", lambda job: dict(job))
    monkeypatch.setattr(jd_finalizer, "_fetch_public_page", lambda url: "")
    monkeypatch.setattr(jd_finalizer, "job_detail_is_live", lambda host, tenant, site, external_path: (True, detail))

    raw = jd_finalizer.resolve_full_jd({
        "external_id": "same-job",
        "source": "workday",
        "company_key": "Eaton",
        "company": "Eaton",
        "title": "Job opening",
        "url": url,
        "original_url": url,
        "ats_provider": "workday",
        "ats_identifier": "eaton|Eaton_Careers",
        "description": "",
        "description_complete": False,
    })

    assert raw["title"] == "Lead AI and Data Engineer"
    assert raw["company_key"] == "Eaton"
    assert raw["location"] == "Beachwood, OH | Mountainside, NJ"
    assert raw["employment_type"] == "Full time"
    assert raw["requisition_id"] == "JR12345"
    assert raw["metadata_resolution_source"] == "workday_cxs"
    assert raw["metadata_verified"] is True
    assert raw["description"] == jd.strip()
    assert raw["description_complete"] is True
    assert raw["description_usable"] is True



def test_noisy_client_rendered_job_recovers_title_and_canonical_record(monkeypatch):
    noisy = (
        "navigation script shell " * 4000
        + " Eaton's Corporate Sector division is currently seeking a Lead AI and Data Engineer. "
        + "Responsibilities build scalable AI and data engineering solutions. "
        + "Requirements Python SQL Azure OpenAI Databricks machine learning. "
        + "Qualifications bachelor's degree and engineering experience. "
    )
    clean = (
        "Responsibilities: build scalable AI and data engineering solutions using Python, SQL, Azure OpenAI and Databricks. "
        "Requirements: software engineering, data engineering, machine learning, cloud architecture and production operations. "
        "Qualifications: bachelor's degree and relevant experience. "
    ) * 8
    base = {
        "external_id":"manual:1",
        "source":"workday",
        "company_key":"Eaton",
        "company":"Eaton",
        "title":"Job opening",
        "url":"https://example.test/eaton/job/1",
        "original_url":"https://example.test/eaton/job/1",
        "ats_provider":"workday",
        "ats_identifier":"eaton|Eaton_Careers",
        "description":noisy,
        "description_complete":True,
        "manual_link":True,
    }
    matched = {
        "title":"Lead AI and Data Engineer",
        "location":"Beachwood, OH | Mountainside, NJ",
        "employment_type":"Full time",
        "requisition_id":"JR12345",
        "original_url":"https://eaton.wd5.myworkdayjobs.com/Eaton_Careers/job/1",
        "url":"https://eaton.wd5.myworkdayjobs.com/Eaton_Careers/job/1",
        "ats_provider":"workday",
        "ats_identifier":"eaton|Eaton_Careers",
        "description":clean,
        "resolver_match_score":1.17,
        "ats_resolution":"verified_employer_source_match",
    }
    monkeypatch.setattr(jd_finalizer,"resolve_original_ats",lambda job:dict(job))
    monkeypatch.setattr(jd_finalizer,"enrich_authoritative_job_metadata",lambda job:dict(job))
    monkeypatch.setattr(jd_finalizer,"resolve_employer_job",lambda job: matched if job.get("title")=="Lead AI and Data Engineer" else None)
    monkeypatch.setattr(jd_finalizer,"_fetch_public_page",lambda url:"")

    out=jd_finalizer.resolve_full_jd(base)

    assert out["title"]=="Lead AI and Data Engineer"
    assert out["description"]==clean.strip()
    assert out["location"]=="Beachwood, OH | Mountainside, NJ"
    assert out["requisition_id"]=="JR12345"
    assert out["metadata_resolution_source"]=="verified_employer_source_match"
    assert out["metadata_verified"] is True
    assert len(out["description"]) < len(noisy)


def test_recover_title_from_full_jd_not_limited_to_first_12k():
    text=("page shell " * 5000) + " Eaton's Corporate Sector division is currently seeking a Lead AI and Data Engineer. Responsibilities follow."
    assert jd_finalizer._recover_title_from_jd_text(text)=="Lead AI and Data Engineer"



def test_exact_ats_detail_jd_beats_long_client_shell(monkeypatch):
    url="https://eaton.eightfold.ai/careers/job/687239519334"
    shell=(
        "navigation client shell " * 15000
        + " responsibilities requirements qualifications skills preferred "
    )
    clean=(
        "Responsibilities: design, develop, and deploy scalable AI and data engineering solutions using Azure OpenAI, Python, SQL, and Databricks. "
        "Requirements: strong software engineering, data engineering, machine learning, cloud architecture, CI/CD, testing, monitoring, and production support. "
        "Qualifications: bachelor's or master's degree and relevant engineering experience. "
    ) * 6
    exact={
        "title":"Lead AI and Data Engineer",
        "company_key":"Eaton",
        "company":"Eaton",
        "url":url,
        "original_url":url,
        "ats_provider":"eightfold",
        "ats_identifier":"eaton",
        "job_id":"687239519334",
        "requisition_id":"JR-1001",
        "description":clean,
        "exact_job_metadata_source":"eightfold_public_api",
    }
    monkeypatch.setattr(jd_finalizer,"resolve_original_ats",lambda job:dict(job))
    monkeypatch.setattr(jd_finalizer,"detect_ats",lambda value:("eightfold","eaton"))
    monkeypatch.setattr(
        jd_finalizer,
        "fetch_exact_job",
        lambda provider,company,source: (exact,"eightfold","eaton"),
    )
    monkeypatch.setattr(jd_finalizer,"_fetch_public_page",lambda value:shell)
    monkeypatch.setattr(jd_finalizer,"_jobposting_metadata",lambda page:{})
    monkeypatch.setattr(jd_finalizer,"_workday_authoritative_metadata",lambda *args:{})

    out=jd_finalizer.resolve_full_jd({
        "external_id":"manual:test",
        "source":"manual_link",
        "company_key":"Eaton",
        "company":"Eaton",
        "title":"Job opening",
        "url":url,
        "original_url":url,
        "description":shell,
        "description_complete":True,
    })

    assert out["title"]=="Lead AI and Data Engineer"
    assert out["description"]==clean.strip()
    assert len(out["description"]) < len(shell)
    assert out["metadata_resolution_source"]=="eightfold_public_api"
    assert out["metadata_verified"] is True
    assert out["requisition_id"]=="JR-1001"
