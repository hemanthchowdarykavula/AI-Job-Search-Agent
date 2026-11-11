from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from app.job_identity import canonical_job_key, identity_keys

DEFAULT_LEDGER=Path("generated/job_ledger.json")

def load_ledger(path=DEFAULT_LEDGER):
    p=Path(path)
    if not p.exists():return {"jobs":{}}
    try:return json.loads(p.read_text(encoding="utf-8"))
    except Exception:return {"jobs":{}}

def save_ledger(ledger,path=DEFAULT_LEDGER):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(ledger,indent=2),encoding="utf-8")

# Fields that must survive compaction because they can affect deduplication,
# retries, application recovery, or the dashboard.
_COMPACT_KEEP_FIELDS={
    "first_seen","last_seen","company","title","source","url","application_status",
    "sources","external_ids","submitted_at","application_result","application_reason",
    "application_blockers","application_result_path","submission_attempted",
    "submission_attempt","resume_retry_count","resume_retry_after","resume_retry_exhausted",
    "application_retry_count","application_retry_after","application_retry_exhausted",
    "retry_exhausted_reason","queue_item","retry_application","retry_job",
    # Keep verification evidence so legacy/in-flight resume retries can be
    # rehydrated even if their nested retry_job was created by an older build.
    "freshness_proof","official_posted_at","official_posted_label","freshness_basis",
    "recovery_scan","discovery_window_hours","live_check",
}

def compact_ledger(ledger):
    """Drop stale non-operational payload fields without changing job identity/state.

    Every job key and alias is retained, so deduplication behavior is unchanged.
    Operational/application rows retain the fields required for retries, recovery,
    submission history, and dashboard display.
    """
    jobs=ledger.get("jobs") or {}
    compacted={}
    removed_fields=0
    for key,row in jobs.items():
        if not isinstance(row,dict):
            compacted[key]=row
            continue
        kept={k:v for k,v in row.items() if k in _COMPACT_KEEP_FIELDS}
        removed_fields+=len(row)-len(kept)
        compacted[key]=kept
    result=dict(ledger)
    result["jobs"]=compacted
    result["aliases"]=dict(ledger.get("aliases") or {})
    return result,{"jobs":len(compacted),"aliases":len(result["aliases"]),"removed_fields":removed_fields}

def _lookup(job,ledger):
    jobs=ledger.get("jobs",{});aliases=ledger.get("aliases",{})
    for candidate in identity_keys(job):
        key=aliases.get(candidate,candidate);row=jobs.get(key)
        if row:return key,row
    return canonical_job_key(job),None

PROCESSED_STATUSES={
    # FINAL_JD_VERIFIED is intentionally NOT terminal: a free discovery/finalization
    # cycle must not prevent a later paid resume-generation cycle from processing it.
    "HOLD_ATS_REVIEW","READY_TO_APPLY",
    "SUBMITTED","SUBMITTED_CONFIRMED","SUBMISSION_ATTEMPTED","MANUAL_ACTION_REQUIRED","SECURITY_BLOCKED","PERMANENT_SKIP",
}

# Verified jobs are resumable work until a validated resume is actually prepared.
RETRYABLE_STATUSES={"RETRY_RESUME_GENERATION","FINAL_JD_VERIFIED"}
MAX_RESUME_RETRIES=3
MAX_APPLICATION_RETRIES=3
RETRY_BACKOFF_MINUTES=(60,120,240)

def _retry_due(row,kind,now=None):
    """Return True when a retry is eligible after bounded exponential backoff."""
    now=now or datetime.now(timezone.utc)
    count=int(row.get(f"{kind}_retry_count") or 0)
    if count>= (MAX_RESUME_RETRIES if kind=="resume" else MAX_APPLICATION_RETRIES):return False
    next_at=row.get(f"{kind}_retry_after")
    if not next_at:return True
    try:return now>=datetime.fromisoformat(next_at)
    except Exception:return True

def retry_metadata(row,kind,now=None):
    """Increment retry count and calculate the next hourly-scheduler retry time."""
    now=now or datetime.now(timezone.utc)
    count=int(row.get(f"{kind}_retry_count") or 0)+1
    max_retries=MAX_RESUME_RETRIES if kind=="resume" else MAX_APPLICATION_RETRIES
    exhausted=count>=max_retries
    delay=RETRY_BACKOFF_MINUTES[min(count-1,len(RETRY_BACKOFF_MINUTES)-1)]
    return {
        f"{kind}_retry_count":count,
        f"{kind}_retry_after":None if exhausted else (now+timedelta(minutes=delay)).isoformat(),
        f"{kind}_retry_exhausted":exhausted,
    }

def seen_or_submitted(job,ledger):
    key,row=_lookup(job,ledger)
    return bool(row and row.get("application_status") in PROCESSED_STATUSES),key,row

RETRY_VERIFICATION_FIELDS=(
    "freshness_proof","official_posted_at","official_posted_label","freshness_basis",
    "recovery_scan","discovery_window_hours","live_check",
)

def retryable_jobs(ledger):
    """Return persisted jobs whose transient resume-generation failure should be retried.

    Older retry payloads may predate verification-field persistence. Rehydrate
    those fields from the parent ledger row when available so a resume retry
    cannot discard posting freshness or live-application evidence.
    """
    out=[]
    for key,row in (ledger.get("jobs") or {}).items():
        if row.get("application_status") not in RETRYABLE_STATUSES:continue
        if not _retry_due(row,"resume"):continue
        stored=row.get("retry_job")
        if isinstance(stored,dict) and stored.get("external_id"):
            payload=dict(stored)
            for field in RETRY_VERIFICATION_FIELDS:
                if payload.get(field) is None and row.get(field) is not None:
                    payload[field]=row.get(field)
            out.append(payload)
    return out

def record_seen(job,ledger,status="DISCOVERED",**extra):
    key,existing=_lookup(job,ledger);now=datetime.now(timezone.utc).isoformat()
    jobs=ledger.setdefault("jobs",{});row=existing or jobs.setdefault(key,{"first_seen":now})
    aliases=ledger.setdefault("aliases",{})
    for alias in identity_keys(job):aliases[alias]=key
    row.update({"last_seen":now,"company":job.get("company_key") or job.get("company"),"title":job.get("title"),
                "source":job.get("source"),"url":job.get("original_url") or job.get("url"),"application_status":status})
    sources=set(row.get("sources") or []);sources.add(job.get("source") or "unknown");row["sources"]=sorted(sources)
    external_ids=set(row.get("external_ids") or []);external_ids.add(job.get("external_id") or "");row["external_ids"]=sorted(x for x in external_ids if x)
    row.update(extra);return key
