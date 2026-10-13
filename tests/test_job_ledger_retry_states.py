from app.job_ledger import load_ledger, retryable_jobs
from app import production_cycle


def test_only_resume_retry_state_can_enter_resume_recovery():
    ledger={"jobs":{
        "ready":{"application_status":"READY_TO_APPLY","retry_job":{"external_id":"ready:1"}},
        "manual":{"application_status":"MANUAL_READY_TO_APPLY","retry_job":{"external_id":"manual:1"}},
        "progress":{"application_status":"APPLICATION_IN_PROGRESS","retry_job":{"external_id":"progress:1"}},
        "application-retry":{"application_status":"RETRY_APPLICATION","retry_job":{"external_id":"application:1"}},
        "resume-retry":{"application_status":"RETRY_RESUME_GENERATION","retry_job":{"external_id":"retry:1"}},
    }}
    rows=retryable_jobs(ledger)
    assert [x["external_id"] for x in rows]==["retry:1"]


def test_legacy_resume_retry_rehydrates_verification_evidence():
    proof={"posted_at":"2026-10-08T09:00:00-04:00","production_cutoff":"2026-10-08T07:30:00-04:00"}
    live={"passed":True,"reason":"reachable"}
    ledger={"jobs":{
        "retry":{
            "application_status":"RETRY_RESUME_GENERATION",
            "retry_job":{"external_id":"retry:1","eligibility":{"experience":{"eligible":True}}},
            "freshness_proof":proof,
            "official_posted_at":"2026-10-08T09:00:00-04:00",
            "official_posted_label":"Today",
            "freshness_basis":"official_employer_posting_date",
            "recovery_scan":True,
            "discovery_window_hours":2.5,
            "live_check":live,
        },
    }}
    rows=retryable_jobs(ledger)
    assert rows[0]["freshness_proof"]==proof
    assert rows[0]["official_posted_at"]=="2026-10-08T09:00:00-04:00"
    assert rows[0]["freshness_basis"]=="official_employer_posting_date"
    assert rows[0]["recovery_scan"] is True
    assert rows[0]["discovery_window_hours"]==2.5
    assert rows[0]["live_check"]==live


def test_resume_retry_payload_preserves_verified_posting_evidence(tmp_path):
    ledger_path=tmp_path/"ledger.json"
    proof={"posted_at":"2026-10-08T09:00:00-04:00","production_cutoff":"2026-10-08T07:30:00-04:00"}
    live={"passed":True,"reason":"reachable","url":"https://example.com/job/1"}
    row={
        "external_id":"workday:test:1",
        "source":"workday",
        "company":"Example Co",
        "title":"Senior Data Engineer",
        "url":"https://example.com/job/1",
        "original_url":"https://example.com/job/1",
        "ats_provider":"workday",
        "ats_identifier":"example",
        "ats_resolution":{"provider":"workday"},
        "application_route":"EXTERNAL_ATS",
        "tailoring_mode":"FULL_JD",
        "description":"Build production data pipelines with Python, SQL, Spark, and Snowflake.",
        "description_complete":True,
        "description_usable":True,
        "employment_type":"Full-Time",
        "location":"United States",
        "eligibility":{"experience":{"eligible":True},"sponsorship":{"eligible":True}},
        "freshness_proof":proof,
        "official_posted_at":"2026-10-08T09:00:00-04:00",
        "official_posted_label":"Today",
        "freshness_basis":"official_employer_posting_date",
        "recovery_scan":False,
        "discovery_window_hours":2.5,
        "live_check":live,
        "requisition_id":"R1",
        "application_questions":[{"label":"Authorized to work?"}],
        "next_action":"RETRY_RESUME_GENERATION",
    }
    production_cycle._sync_manifest([row],ledger_path,"cycle-1")
    ledger=load_ledger(ledger_path)
    stored=next(iter(ledger["jobs"].values()))["retry_job"]
    assert stored["freshness_proof"]==proof
    assert stored["official_posted_at"]=="2026-10-08T09:00:00-04:00"
    assert stored["official_posted_label"]=="Today"
    assert stored["freshness_basis"]=="official_employer_posting_date"
    assert stored["discovery_window_hours"]==2.5
    assert stored["live_check"]==live
    assert stored["requisition_id"]=="R1"
    assert stored["application_questions"]==[{"label":"Authorized to work?"}]


def test_retry_with_complete_verification_skips_reverification(monkeypatch,tmp_path):
    raw={
        "external_id":"retry:verified",
        "eligibility":{"experience":{"eligible":True}},
        "freshness_proof":{"posted_at":"2026-10-08T09:00:00-04:00","production_cutoff":"2026-10-08T07:30:00-04:00"},
        "live_check":{"passed":True,"reason":"reachable"},
    }
    monkeypatch.setattr(production_cycle,"retryable_jobs",lambda ledger:[raw])
    monkeypatch.setattr(
        production_cycle,
        "finalize_report",
        lambda *a,**k: (_ for _ in ()).throw(AssertionError("complete retry evidence should not be reverified")),
    )
    ledger_path=tmp_path/"ledger.json"
    ledger_path.write_text('{"jobs":{}}',encoding="utf-8")
    rows=production_cycle._retry_items_from_ledger(ledger_path,hours=2,since="2026-10-08T07:30:00-04:00")
    assert rows==[{"action":"FINAL_JD_VERIFIED","job":raw,"eligibility":raw["eligibility"]}]


def test_legacy_retry_without_proof_is_reverified_before_resume(monkeypatch,tmp_path):
    raw={
        "external_id":"retry:legacy",
        "source":"workday",
        "company_key":"Example Co",
        "title":"Senior Data Engineer",
        "url":"https://example.com/job/1",
        "eligibility":{"experience":{"eligible":True}},
    }
    verified=dict(raw)
    verified["freshness_proof"]={
        "posted_at":"2026-10-08T09:00:00-04:00",
        "production_cutoff":"2026-10-08T07:30:00-04:00",
    }
    verified["live_check"]={"passed":True,"reason":"reachable"}
    captured={}
    monkeypatch.setattr(production_cycle,"retryable_jobs",lambda ledger:[raw])

    def fake_finalize(report_path,output_path,hours=24,now=None,since=None):
        import json
        from pathlib import Path
        payload=json.loads(Path(report_path).read_text(encoding="utf-8"))
        captured["action"]=payload["results"][0]["action"]
        captured["external_id"]=payload["results"][0]["job"]["external_id"]
        captured["hours"]=hours
        captured["since"]=since
        return {
            "finalized":1,
            "held_or_rejected":0,
            "results":[{"action":"FINAL_JD_VERIFIED","job":verified,"eligibility":raw["eligibility"]}],
            "rejections":[],
        }

    monkeypatch.setattr(production_cycle,"finalize_report",fake_finalize)
    ledger_path=tmp_path/"ledger.json"
    ledger_path.write_text('{"jobs":{}}',encoding="utf-8")
    rows=production_cycle._retry_items_from_ledger(
        ledger_path,hours=5,since="2026-10-08T07:52:31-04:00"
    )
    assert captured=={
        "action":"ELIGIBLE_FOR_RESUME",
        "external_id":"retry:legacy",
        "hours":5,
        "since":"2026-10-08T07:52:31-04:00",
    }
    assert rows[0]["job"]["freshness_proof"]==verified["freshness_proof"]
    assert rows[0]["job"]["live_check"]["passed"] is True


def test_legacy_retry_reverification_hold_never_reaches_resume(monkeypatch,tmp_path):
    raw={
        "external_id":"retry:stale",
        "company_key":"Example Co",
        "title":"Senior Data Engineer",
        "eligibility":{"experience":{"eligible":True}},
    }
    monkeypatch.setattr(production_cycle,"retryable_jobs",lambda ledger:[raw])
    monkeypatch.setattr(
        production_cycle,
        "finalize_report",
        lambda *a,**k:{
            "finalized":0,
            "held_or_rejected":1,
            "results":[],
            "rejections":[{
                "job":raw,
                "action":"REJECT_STALE_OFFICIAL_POSTING",
                "reason":"Selected posting date is outside the requested freshness window.",
            }],
        },
    )
    ledger_path=tmp_path/"ledger.json"
    ledger_path.write_text('{"jobs":{"retry":{"application_status":"RETRY_RESUME_GENERATION","retry_job":{"external_id":"retry:stale","company_key":"Example Co","title":"Senior Data Engineer","eligibility":{"experience":{"eligible":True}}}}}}',encoding="utf-8")
    assert production_cycle._retry_items_from_ledger(ledger_path,hours=2)==[]
    ledger=load_ledger(ledger_path)
    stored=next(iter(ledger["jobs"].values()))
    assert stored["application_status"]=="PERMANENT_SKIP"
    assert stored["retry_job"] is None
    assert "REJECT_STALE_OFFICIAL_POSTING" in stored["retry_exhausted_reason"]
