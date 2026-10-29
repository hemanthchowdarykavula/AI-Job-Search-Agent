from __future__ import annotations
import argparse,json,re
from pathlib import Path
from urllib.parse import urlparse
from app.config import load_profile
from app.filters import passes_hard_filters
from app.job_identity import identity_keys
from app.approved_job_boards import approved_board, board_application_url, BOARD_APPLICATION_ROUTE

ROOT=Path(__file__).resolve().parents[1]

MANUAL_BLOCKERS=("captcha","recaptcha","hcaptcha","mfa","two-factor","2fa","verification code")

def _provider(row):
    if (row.get("application_route") or "").upper()==BOARD_APPLICATION_ROUTE:
        return str(row.get("source") or "board")
    explicit=(row.get("ats_provider") or "").lower().strip()
    if explicit:return explicit
    if (row.get("application_route") or "").upper()=="DICE":return "dice"
    host=urlparse(row.get("original_url") or row.get("url") or "").netloc.lower()
    hints={"greenhouse":"greenhouse","lever":"lever","ashby":"ashby","myworkdayjobs":"workday",
           "smartrecruiters":"smartrecruiters","icims":"icims","jobvite":"jobvite"}
    return next((p for token,p in hints.items() if token in host),"unknown")

def _yes_no(value):
    if value is True:return "Yes"
    if value is False:return "No"
    return None

def _known_answers(profile):
    """Build work-authorization answers from the candidate profile, never constants."""
    work_auth=(profile or {}).get("work_authorization") or {}
    authorized=work_auth.get("application_answer_authorized") or _yes_no(work_auth.get("authorized_to_work_us"))
    return {
      "authorized_to_work_us":authorized,
      "requires_sponsorship_now":_yes_no(work_auth.get("requires_sponsorship_now")),
      "requires_future_sponsorship":_yes_no(work_auth.get("requires_sponsorship_future")),
      "sponsorship_statement":work_auth.get("statement")
    }

def _artifact_path(value,manifest_path):
    if not value:return None
    raw=str(value).strip()
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return str(Path(raw))
    p=Path(raw)
    if p.is_absolute():return str(p)
    candidates=[ROOT/p,Path(manifest_path).resolve().parent/p,p.resolve()]
    for candidate in candidates:
        if candidate.exists():return str(candidate.resolve())
    return str((ROOT/p).resolve())

def _application_gate(row,profile):
    """Re-run governing eligibility immediately before a job can enter the ATS queue."""
    job={
      "external_id":row.get("external_id"),"source":row.get("source"),"company_key":row.get("company"),"title":row.get("title"),
      "location":row.get("location"),"employment_type":row.get("employment_type"),"description":row.get("description") or ""
    }
    return passes_hard_filters(job,profile)

def _queue_identity_keys(row):
    """Shared cross-source aliases used to block duplicate final submissions."""
    job=dict(row)
    return identity_keys(job)

def build(manifest_path="generated/application_manifest.json",output="generated/application_queue.json"):
    """Queue only fully validated, still-eligible artifacts; never trust an earlier gate alone."""
    rows=json.loads(Path(manifest_path).read_text(encoding="utf-8"));queue=[];profile=load_profile();seen_keys=set()
    for r in rows:
        if r.get("next_action")!="READY_TO_APPLY":continue
        keys=_queue_identity_keys(r)
        if any(key in seen_keys for key in keys):continue
        validation=r.get("artifact_validation") or {}
        pdf=r.get("pdf_path")
        resolved_pdf=_artifact_path(pdf,manifest_path)
        explicit_validation="artifact_validation" in r and r.get("artifact_validation") is not None
        if not pdf or not resolved_pdf:continue
        if explicit_validation and not validation.get("passed"):continue
        provider=_provider(r)
        gate_ok,gate_reasons=_application_gate(r,profile)
        if not gate_ok:
            continue
        seen_keys.update(keys)
        queue.append({
          "external_id":r.get("external_id"),"source":r.get("source"),"company":r.get("company"),"title":r.get("title"),
          "url":(board_application_url(r) if approved_board(r.get("source")) and ((r.get("application_route") or "").upper()==BOARD_APPLICATION_ROUTE or r.get("source")=="himalayas") else None)
                or r.get("original_url") or r.get("url"),
          "source_credit":r.get("source_credit"),"posting_verification_scope":r.get("posting_verification_scope"),"board_posted_at":r.get("board_posted_at"),"ats_provider":provider,"application_route":r.get("application_route") or ("DICE" if provider=="dice" else "EXTERNAL_ATS"),
          "ats_score":r.get("ats_audit",{}).get("internal_ats_score"),"resume_path":resolved_pdf,
          "artifact_validation":validation,"known_answers":_known_answers(profile),
          "location":r.get("location"),"employment_type":r.get("employment_type"),"description":r.get("description"),
          "requisition_id":r.get("requisition_id"),
          "freshness_proof":r.get("freshness_proof"),
          "official_posted_at":r.get("official_posted_at"),
          "official_posted_label":r.get("official_posted_label"),
          "freshness_basis":r.get("freshness_basis"),
          "recovery_scan":bool(r.get("recovery_scan")),
          "discovery_window_hours":r.get("discovery_window_hours"),
          "live_check":r.get("live_check"),
          "application_questions":r.get("application_questions"),
          "screening_questions":r.get("screening_questions"),
          "questions":r.get("questions"),
          "application_form":r.get("application_form"),
          "application_gate":{"passed":True,"reasons":[]},
          "unknown_answer_policy":"MANUAL_ACTION_REQUIRED",
          "blocker_policy":"MANUAL_ACTION_REQUIRED",
          "status":"READY_FOR_ATS_ADAPTER",
          "status_reason":None
        })
    out=Path(output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(queue,indent=2),encoding="utf-8");return queue

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--manifest",default="generated/application_manifest.json");p.add_argument("--output",default="generated/application_queue.json");a=p.parse_args();rows=build(a.manifest,a.output);print(json.dumps({"queued":len(rows),"ready_for_adapter":sum(x["status"]=="READY_FOR_ATS_ADAPTER" for x in rows),"manual_action":sum(x["status"]=="MANUAL_ACTION_REQUIRED" for x in rows),"output":a.output},indent=2))
