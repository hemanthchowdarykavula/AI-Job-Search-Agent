import json
from app import dashboard


def test_cycle_snapshot_includes_only_ready_jobs_without_duplicates(monkeypatch,tmp_path):
    monkeypatch.setattr(dashboard,"CYCLES",tmp_path)
    cycle="20260926T160000Z"
    rows=[
      {"external_id":"lever:1","company":"Example Inc","title":"Data Engineer","location":"US","requisition_id":"REQ-1","next_action":"READY_TO_APPLY"},
      {"external_id":"dice:99","company":"Example Inc","title":"Data Engineer","location":"US","requisition_id":"REQ-1","next_action":"READY_TO_APPLY"},
      {"external_id":"manatal:2","company":"Another Co","title":"Senior Data Engineer","location":"Remote","requisition_id":"REQ-2","next_action":"READY_TO_APPLY"},
      {"external_id":"held:3","company":"Held Co","title":"Data Engineer","next_action":"HOLD_ARTIFACT_VALIDATION"},
    ]
    (tmp_path/f"{cycle}_manifest.json").write_text(json.dumps(rows),encoding="utf-8")
    snapshot=dashboard._cycle_snapshot(cycle)
    assert len(snapshot)==2
    assert all(x["next_action"]=="READY_TO_APPLY" for x in snapshot)
    assert {x["company"] for x in snapshot}=={"Example Inc","Another Co"}


def test_pipeline_runs_exposes_single_ready_count(monkeypatch,tmp_path):
    monkeypatch.setattr(dashboard,"CYCLES",tmp_path)
    cycle="20260926T160000Z"
    (tmp_path/f"{cycle}_summary.json").write_text(json.dumps({"cycle_id":cycle,"ready_to_apply":5}),encoding="utf-8")
    run=dashboard._pipeline_runs()[0]
    assert run["ready"]==5
    assert "manual_ready" not in run


def test_pipeline_runs_exposes_funnel_source_health_and_issue_names(monkeypatch,tmp_path):
    monkeypatch.setattr(dashboard,"CYCLES",tmp_path)
    cycle="20261007T140000Z"
    summary={
        "cycle_id":cycle,
        "production_cutoff":"2026-10-07T12:00:00+00:00",
        "discovered":100,"fresh_verified_within_window":20,"filtered_out":15,
        "preliminary_eligible":5,"final_jd_verified":3,"ready_to_apply":2,
        "filter_reason_counts":{"wrong_job_family":10,"experience_mismatch":3},
        "source_unit_status":{
            "workday:Good":{"source":"workday","company":"Good","status":"OK"},
            "icims:Broken":{"source":"icims","company":"Broken","status":"ERROR","error":"HTTP 503"},
            "oracle:Dead":{"source":"oracle","company":"Dead","status":"SKIPPED_HARD_FAILURE","error":"HTTP 404"},
            "ukg:Slow":{"source":"ukg","company":"Slow","status":"BACKOFF_TRANSIENT","error":"TRANSIENT_BACKOFF"},
        },
        "source_repair_queue":{"pending":1,"repaired":4,"failed":0},
    }
    (tmp_path/f"{cycle}_summary.json").write_text(json.dumps(summary),encoding="utf-8")
    run=dashboard._pipeline_runs()[0]
    assert run["fresh"]==20
    assert run["source_ok"]==1
    assert run["source_failed"]==1
    assert run["source_backoff"]==1
    assert run["source_hard_skipped"]==1
    assert run["repair_pending"]==1
    assert any(x["company"]=="Broken" for x in run["source_issues"])


def test_discovery_dashboard_all_job_sources_show_open_job_action():
    """Source board and official ATS listings must share one visible link label."""
    html = dashboard.dashboard().body.decode("utf-8")
    assert "Apply on Job Board" not in html
    assert '"Open Job"' in html
    # The existing job-specific URL and safe external-link attributes stay intact.
    assert 'esc(r.url)' in html
    assert 'rel="noopener noreferrer"' in html
