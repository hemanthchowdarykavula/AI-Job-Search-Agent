from __future__ import annotations
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from app.daily_runner import run as discover_and_filter
from app.jd_finalizer import finalize_report
from app.source_window_finalizer import finalize_report_by_source
from app.batch_prepare import prepare
from app.job_ledger import load_ledger,save_ledger,record_seen,retryable_jobs,retry_metadata,_lookup
from app.application_queue import build as build_application_queue
from app.source_reliability import update_from_cycle as update_source_reliability, summary as source_reliability_summary
from app.source_repair import update_from_reliability as update_source_repair_queue, summary as source_repair_summary

ROOT=Path(__file__).resolve().parent.parent

def _write(path,payload):
 p=ROOT/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(payload,indent=2),encoding="utf-8");return str(p)

def _sync_finalized(rows,ledger_path,cycle_id=None):
 ledger=load_ledger(ledger_path)
 for row in rows:
  raw=row.get("job") or row.get("raw") or row
  status=row.get("action") or raw.get("action")
  if not status:continue
  record_seen(raw,ledger,status,
              original_url=raw.get("original_url"),
              ats_provider=raw.get("ats_provider"),
              ats_identifier=raw.get("ats_identifier"),
              requisition_id=raw.get("requisition_id") or raw.get("job_id"),
              jd_hash=raw.get("jd_hash"),
              description_complete=raw.get("description_complete"),
              cycle_id=cycle_id)
 save_ledger(ledger,ledger_path)

def _retry_items_from_ledger(ledger_path,hours=24,now=None,since=None):
 """Return resume retries, re-verifying legacy rows that lost safety evidence."""
 ledger_data=load_ledger(ledger_path)
 items=[];needs_reverify=[]
 for raw in retryable_jobs(ledger_data):
  elig=raw.get("eligibility")
  if not isinstance(elig,dict):continue
  proof=raw.get("freshness_proof") or {}
  live=raw.get("live_check") or {}
  # A retry may bypass discovery, but it must never bypass the final posting
  # safety gates. New retry records preserve these fields; legacy records that
  # lost them are sent through the same finalizer used by fresh jobs.
  if proof.get("posted_at") and proof.get("production_cutoff") and live.get("passed") is True:
   items.append({"action":"FINAL_JD_VERIFIED","job":raw,"eligibility":elig})
  else:
   needs_reverify.append(raw)
 if needs_reverify:
  with TemporaryDirectory(prefix="resume-retry-reverify-") as tmp:
   report_path=Path(tmp)/"eligible.json"
   output_path=Path(tmp)/"finalized.json"
   report_path.write_text(json.dumps({"results":[
    {"action":"ELIGIBLE_FOR_RESUME","job":raw,"eligibility":raw.get("eligibility")}
    for raw in needs_reverify
   ]},indent=2),encoding="utf-8")
   reverified=finalize_report(
    str(report_path),str(output_path),hours=hours,now=now,since=since
   )
  items.extend(reverified.get("results") or [])
  terminal_retry_actions={"REJECT_STALE_OFFICIAL_POSTING","REJECT_DEAD_JOB","SKIP_FINAL_ELIGIBILITY"}
  ledger_changed=False
  for rejected in reverified.get("rejections") or []:
   job=rejected.get("job") or {}
   action=rejected.get("action") or "HOLD"
   reason=rejected.get("reason") or "; ".join(rejected.get("reasons") or [])
   print(
    "RETRY REVERIFY HOLD | {} | {} | {} | {}".format(
     job.get("company_key") or job.get("company") or "Unknown",
     job.get("title") or "",
     action,
     reason,
    ),
    flush=True,
   )
   # A stale/dead/ineligible posting cannot become valid by retrying the same
   # resume generation again next slot. Mark it terminal so it does not consume
   # repeated live verification work forever. Transient HOLD_* states remain
   # retryable because source/JD/application evidence may recover later.
   if action in terminal_retry_actions:
    record_seen(
     job,ledger_data,"PERMANENT_SKIP",
     retry_job=None,
     application_reason=f"Resume retry re-verification terminal: {action}: {reason}",
     retry_exhausted_reason=f"Terminal re-verification result: {action}",
    )
    ledger_changed=True
  if ledger_changed:
   save_ledger(ledger_data,ledger_path)
 return items

def _sync_manifest(rows,ledger_path,cycle_id=None):
 ledger=load_ledger(ledger_path)
 for row in rows:
  job={"external_id":row.get("external_id"),"source":row.get("source"),"company_key":row.get("company"),"title":row.get("title"),"url":row.get("url")}
  extra={"resume_path":row.get("resume_path"),"pdf_path":row.get("pdf_path"),"ats_audit":row.get("ats_audit"),"artifact_validation":row.get("artifact_validation"),"cycle_id":cycle_id,
         "freshness_proof":row.get("freshness_proof"),"official_posted_at":row.get("official_posted_at"),
         "official_posted_label":row.get("official_posted_label"),"freshness_basis":row.get("freshness_basis"),
         "recovery_scan":bool(row.get("recovery_scan")),"discovery_window_hours":row.get("discovery_window_hours"),
         "live_check":row.get("live_check"),"employment_type":row.get("employment_type"),"location":row.get("location"),
         "description":row.get("description"),"requisition_id":row.get("requisition_id")}
  status=row.get("next_action") or "PREPARED"
  if row.get("next_action")=="READY_TO_APPLY":
   pdf_path=row.get("pdf_path")
   validation=row.get("artifact_validation") or {}
   explicit_validation="artifact_validation" in row and row.get("artifact_validation") is not None
   if pdf_path and Path(pdf_path).suffix.lower()==".pdf" and (not explicit_validation or validation.get("passed")):
    queue_payload={
     "external_id":row.get("external_id"),"source":row.get("source"),"company":row.get("company"),"title":row.get("title"),
     "url":row.get("original_url") or row.get("url"),"ats_provider":row.get("ats_provider"),"application_route":row.get("application_route"),
     "resume_path":pdf_path,"artifact_validation":row.get("artifact_validation"),
     "freshness_proof":row.get("freshness_proof"),"official_posted_at":row.get("official_posted_at"),
     "official_posted_label":row.get("official_posted_label"),"freshness_basis":row.get("freshness_basis"),
     "recovery_scan":bool(row.get("recovery_scan")),"discovery_window_hours":row.get("discovery_window_hours"),
     "live_check":row.get("live_check"),
     "status":"READY_FOR_ATS_ADAPTER",
    }
    if queue_payload.get("external_id"):extra["queue_item"]=queue_payload
   else:
    status="HOLD_ARTIFACT_VALIDATION"
    extra["queue_item"]=None
    extra["application_reason"]="Validated PDF resume is required before application."
  if row.get("next_action")=="RETRY_RESUME_GENERATION":
   _,existing=_lookup(job,ledger)
   meta=retry_metadata(existing or {},"resume")
   extra.update(meta)
   if meta["resume_retry_exhausted"]:
    status="MANUAL_ACTION_REQUIRED"
    extra["retry_job"]=None
    extra["retry_exhausted_reason"]="Resume generation retry limit reached"
   else:
    extra["retry_job"]={
    "external_id":row.get("external_id"),"source":row.get("source"),"company_key":row.get("company"),"title":row.get("title"),
    "url":row.get("url"),"original_url":row.get("original_url"),"ats_provider":row.get("ats_provider"),"ats_identifier":row.get("ats_identifier"),
    "ats_resolution":row.get("ats_resolution"),"application_route":row.get("application_route"),"tailoring_mode":row.get("tailoring_mode"),
    "description":row.get("description"),"description_complete":row.get("description_complete"),"description_usable":row.get("description_usable"),
    "employment_type":row.get("employment_type"),"location":row.get("location"),"eligibility":row.get("eligibility"),
    # Resume retry must preserve the already-verified posting/application evidence.
    # Losing these fields makes a successfully regenerated resume fail production
    # acceptance even though the JD was already finalized and verified.
    "freshness_proof":row.get("freshness_proof"),"official_posted_at":row.get("official_posted_at"),
    "official_posted_label":row.get("official_posted_label"),"freshness_basis":row.get("freshness_basis"),
    "recovery_scan":bool(row.get("recovery_scan")),"discovery_window_hours":row.get("discovery_window_hours"),
    "live_check":row.get("live_check"),"requisition_id":row.get("requisition_id"),
    "application_questions":row.get("application_questions"),"screening_questions":row.get("screening_questions"),
    "questions":row.get("questions"),"application_form":row.get("application_form")
    }
  record_seen(job,ledger,status,**extra)
 save_ledger(ledger,ledger_path)

def run_cycle(sources="data/job_sources.json",hours=24,ledger="generated/job_ledger.json",generate_resumes=False,limit=None,external_id=None,since=None,scan_now=None,source_since=None,source_hours=None,source_unit_hours=None):
 stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
 eligible_rel=f"generated/cycles/{stamp}_eligible.json"
 finalized_rel=f"generated/cycles/{stamp}_finalized.json"
 manifest_rel=f"generated/cycles/{stamp}_manifest.json"
 queue_rel=f"generated/cycles/{stamp}_application_queue.json"
 discovery=discover_and_filter(sources,hours,ledger_path=ledger,since=since,scan_now=scan_now,source_since=source_since,source_hours=source_hours,source_unit_hours=source_unit_hours)
 _write(eligible_rel,discovery)
 # Persist per-source reliability after every real cycle. This is independent
 # from job eligibility and only controls retries/repair scheduling.
 update_source_reliability(discovery.get("source_unit_status") or {},now=scan_now)
 update_source_repair_queue()
 if source_hours:
  finalized=finalize_report_by_source(str(ROOT/eligible_rel),str(ROOT/finalized_rel),hours=hours,now=scan_now,source_hours=source_hours,since=since)
 else:
  finalized=finalize_report(str(ROOT/eligible_rel),str(ROOT/finalized_rel),hours=hours,now=scan_now,since=since)
 _sync_finalized(finalized.get("jobs") or finalized.get("results") or [],ledger,stamp)
 retry_items=_retry_items_from_ledger(ledger,hours=hours,now=scan_now,since=since) if generate_resumes else []
 finalized_results=list(finalized.get("results") or finalized.get("jobs") or [])
 retry_ids={x["job"].get("external_id") for x in retry_items}
 existing_ids={(x.get("job") or {}).get("external_id") for x in finalized_results}
 for item in retry_items:
  if item["job"].get("external_id") not in existing_ids:
   finalized_results.append(item)
 if retry_items:
  finalized["results"]=finalized_results
  finalized["finalized"]=sum(1 for x in finalized_results if x.get("action")=="FINAL_JD_VERIFIED")
  _write(finalized_rel,finalized)
 manifest=[]
 if generate_resumes and finalized.get("finalized"):
  manifest=prepare(str(ROOT/finalized_rel),str(ROOT/manifest_rel),external_id=external_id,limit=limit)
  _sync_manifest(manifest,ledger,stamp)
 queue=build_application_queue(str(ROOT/manifest_rel),str(ROOT/queue_rel)) if manifest else []
 # Dashboard-facing eligibility is intentionally the final application-ready
 # count. Preliminary filter matches remain available in the eligible report,
 # but are not presented as "eligible" until JD/live-route verification,
 # resume generation, and artifact validation have all succeeded.
 manifest_ready_count=sum(x.get("next_action")=="READY_TO_APPLY" for x in manifest)
 queued_ready_count=sum(x.get("status")=="READY_FOR_ATS_ADAPTER" for x in queue)
 summary={"cycle_id":stamp,"scan_window_hours":hours,"production_cutoff":since,"discovered":discovery.get("discovered",0),"eligible":queued_ready_count,
          "fresh_verified_within_window":discovery.get("fresh_verified_within_hours",0),
          "older_or_unverified":discovery.get("older_or_unverified",0),
          "filtered_out":discovery.get("filtered_out",0),
          "filter_reason_counts":discovery.get("filter_reason_counts",{}),
          "preliminary_eligible":discovery.get("eligible",0),
          "final_jd_verified":finalized.get("finalized",0),"held_or_rejected":finalized.get("held_or_rejected",0),
          "resume_generation_enabled":generate_resumes,"prepared":len(manifest),
          "manifest_ready_to_apply":manifest_ready_count,
          "ready_to_apply":queued_ready_count,
          "hold_ats_review":sum(x.get("next_action")=="HOLD_ATS_REVIEW" for x in manifest),
          "hold_artifact_validation":sum(x.get("next_action")=="HOLD_ARTIFACT_VALIDATION" for x in manifest),
          "retry_resume_generation":sum(x.get("next_action")=="RETRY_RESUME_GENERATION" for x in manifest),
          "eligible_report":eligible_rel,"finalized_report":finalized_rel,
          "manifest":manifest_rel if generate_resumes else None,
          "application_queue":queue_rel if manifest else None,
          "queued_for_application":queued_ready_count,
          "manual_application_action":sum(x.get("status")=="MANUAL_ACTION_REQUIRED" for x in queue),"source_status":discovery.get("source_status",{}),
          "source_errors":discovery.get("source_errors",{}),"source_unit_status":discovery.get("source_unit_status",{}),
          "coverage":discovery.get("coverage",{}),
          "source_reliability":source_reliability_summary(),
          "source_repair_queue":source_repair_summary()}
 _write(f"generated/cycles/{stamp}_summary.json",summary)
 return summary

if __name__=="__main__":
 p=argparse.ArgumentParser()
 p.add_argument("--sources",default="data/job_sources.json");p.add_argument("--hours",type=int,default=24)
 p.add_argument("--ledger",default="generated/job_ledger.json")
 p.add_argument("--generate-resumes",action="store_true",help="Enable paid LLM resume generation. Omit for free discovery/finalization dry runs.")
 p.add_argument("--limit",type=int,help="Optional resume-generation cap for controlled validation.")
 p.add_argument("--external-id",help="Generate a resume only for the matching finalized job external_id.")
 a=p.parse_args();print(json.dumps(run_cycle(a.sources,a.hours,a.ledger,a.generate_resumes,a.limit,a.external_id),indent=2))