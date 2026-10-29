from __future__ import annotations
import argparse, json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from pathlib import Path
from app.config import load_profile
from app.filters import passes_hard_filters
from app.approved_job_boards import approved_board, board_url_is_valid, BOARD_APPLICATION_ROUTE

def audit(summary_path:str)->dict:
    p=Path(summary_path)
    summary=json.loads(p.read_text(encoding="utf-8"))
    failures=[]
    ready=int(summary.get("ready_to_apply") or 0)
    queued=int(summary.get("queued_for_application") or 0)
    eligible=int(summary.get("eligible") or 0)
    manifest_ready=int(summary.get("manifest_ready_to_apply") or 0)
    if not (ready==queued==eligible):
        failures.append(f"ready/queued/eligible mismatch: {ready}/{queued}/{eligible}")
    if ready>manifest_ready:
        failures.append(f"queued ready exceeds validated manifest ready: {ready}>{manifest_ready}")
    if summary.get("resume_generation_enabled") and summary.get("prepared",0)<manifest_ready:
        failures.append("manifest ready exceeds prepared artifacts")
    if int(summary.get("manual_application_action") or 0):
        failures.append("manual application state is not allowed in the ready-only production flow")
    queue_path=summary.get("application_queue")
    job_checks=[]
    if ready:
        if not queue_path or not Path(queue_path).exists():
            failures.append("ready jobs exist but application queue is missing")
        else:
            queue=json.loads(Path(queue_path).read_text(encoding="utf-8"))
            if len(queue)!=ready:
                failures.append(f"queue length mismatch: {len(queue)}!={ready}")
            ids=set()
            strict_evidence=bool(summary.get("production_cutoff"))
            profile=load_profile() if strict_evidence else {}
            summary_cutoff=None
            if strict_evidence:
                try:summary_cutoff=datetime.fromisoformat(str(summary.get("production_cutoff")).replace("Z","+00:00")).astimezone(timezone.utc)
                except Exception:failures.append("production cutoff is not parseable")
            for row in queue:
                eid=row.get("external_id")
                checks={"external_id":eid,"passed":True,"failures":[]}
                def fail(message):
                    failures.append(message);checks["failures"].append(message);checks["passed"]=False
                if not eid or eid in ids: fail(f"duplicate/missing queue external_id: {eid}")
                ids.add(eid)
                if row.get("status")!="READY_FOR_ATS_ADAPTER": fail(f"invalid queue status for {eid}")
                url=row.get("url") or ""
                host=(urlparse(url).netloc or "").lower()
                board_route=(row.get("application_route") or "").upper()==BOARD_APPLICATION_ROUTE
                if board_route:
                    if not approved_board(row.get("source")) or not board_url_is_valid(row.get("source"),url):
                        fail(f"invalid approved board application destination for {eid}: {host or 'missing'}")
                    if row.get("posting_verification_scope")!="JOB_BOARD_ONLY":
                        fail(f"board listing verification scope missing for {eid}")
                    if (row.get("freshness_basis") or "") not in {"job_board_posting_date","job_board_last_updated"}:
                        fail(f"board timestamp was not validated for {eid}")
                else:
                    aggregator_hosts=("dice.com","indeed.com","linkedin.com","ziprecruiter.com","monster.com",
                                      "wellfound.com","builtin.com","ycombinator.com","workatastartup.com",
                                      "hiringcafe.com","himalayas.app","welcometothejungle.com")
                    if not url or any(host==h or host.endswith("."+h) for h in aggregator_hosts):
                        fail(f"non-authoritative application destination for {eid}: {host or 'missing'}")
                if not (row.get("application_gate") or {}).get("passed"): fail(f"application gate not passed for {eid}")
                resume=row.get("resume_path")
                if not resume or Path(resume).suffix.lower()!=".pdf" or not Path(resume).is_file(): fail(f"validated PDF missing for {eid}")
                validation=row.get("artifact_validation")
                if validation and not validation.get("passed"): fail(f"artifact validation failed for {eid}")

                if strict_evidence:
                    hard_ok,hard_reasons=passes_hard_filters({
                        "external_id":eid,"source":row.get("source"),"company_key":row.get("company"),
                        "title":row.get("title"),"location":row.get("location"),
                        "employment_type":row.get("employment_type"),"description":row.get("description") or "",
                        "application_questions":row.get("application_questions"),
                        "screening_questions":row.get("screening_questions"),
                        "questions":row.get("questions"),
                        "application_form":row.get("application_form"),
                    },profile)
                    if not hard_ok:
                        fail(f"final hard-filter audit failed for {eid}: {'; '.join(hard_reasons)}")

                    proof=row.get("freshness_proof") or {}
                    posted=proof.get("posted_at") or row.get("official_posted_at")
                    proof_cutoff=proof.get("production_cutoff")
                    if not posted or not proof_cutoff:
                        fail(f"freshness proof missing for {eid}")
                    else:
                        try:
                            posted_dt=datetime.fromisoformat(str(posted).replace("Z","+00:00")).astimezone(timezone.utc)
                            cutoff_dt=datetime.fromisoformat(str(proof_cutoff).replace("Z","+00:00")).astimezone(timezone.utc)
                            checked_raw=proof.get("checked_at")
                            checked_dt=datetime.fromisoformat(str(checked_raw).replace("Z","+00:00")).astimezone(timezone.utc) if checked_raw else datetime.now(timezone.utc)
                            if summary_cutoff and abs((cutoff_dt-summary_cutoff).total_seconds())>1:
                                fail(f"freshness cutoff mismatch for {eid}")
                            if posted_dt<cutoff_dt or posted_dt>checked_dt+timedelta(minutes=10):
                                fail(f"freshness proof outside production window for {eid}: {posted}")
                        except Exception:
                            fail(f"freshness proof is not parseable for {eid}")

                    live=row.get("live_check") or {}
                    if not live.get("passed"):
                        fail(f"live application proof missing for {eid}")

                    expected_future=((profile.get("work_authorization") or {}).get("requires_sponsorship_future"))
                    answers=row.get("known_answers") or {}
                    if expected_future is True and answers.get("requires_future_sponsorship")!="Yes":
                        fail(f"future sponsorship answer mismatch for {eid}")
                    if expected_future is False and answers.get("requires_future_sponsorship")!="No":
                        fail(f"future sponsorship answer mismatch for {eid}")
                job_checks.append(checks)
    result={"passed":not failures,"cycle_id":summary.get("cycle_id"),"ready_to_apply":ready,"failures":failures,"job_checks":job_checks}
    out=p.with_name(p.stem+"_acceptance.json")
    out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    if failures: raise SystemExit("Production acceptance failed: "+" | ".join(failures))
    print(json.dumps(result,indent=2))
    return result

def latest_summary(root="generated/cycles")->Path:
    rows=sorted(Path(root).glob("*_summary.json"))
    if not rows: raise SystemExit("No production cycle summary found")
    return rows[-1]

if __name__=="__main__":
    ap=argparse.ArgumentParser();ap.add_argument("--summary")
    a=ap.parse_args();audit(a.summary or str(latest_summary()))
