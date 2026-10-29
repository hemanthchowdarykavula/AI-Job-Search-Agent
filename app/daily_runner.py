from __future__ import annotations
import argparse,json,re
from datetime import datetime,timezone
from collections import Counter
from pathlib import Path
from app.config import load_profile
from app.discovery import discover, ALL_ATS_PROVIDERS
from app.freshness import fresh_jobs
from app.filters import passes_hard_filters
from app.eligibility import two_category_filter
from app.job_ledger import load_ledger, save_ledger, seen_or_submitted, record_seen
from app.target_companies import match_target
from app.company_registry import load as load_company_registry, save as save_company_registry, learn_from_jobs as learn_companies_from_jobs
from app.job_identity import identity_keys

ROOT=Path(__file__).resolve().parent.parent

def load_sources(path):return json.loads(Path(path).read_text(encoding="utf-8"))

def _reason_key(reason):
    r=(reason or "").lower()
    if "data-engineering job family" in r or "title/jd outside" in r or "title outside" in r:return "wrong_job_family"
    if "experience requirement" in r:return "experience_mismatch"
    if "location outside united states" in r:return "location_outside_us"
    if "employment type outside" in r:return "employment_type_mismatch"
    if "us citizenship required" in r:return "citizenship_required"
    if "security/public-trust clearance required" in r:return "clearance_required"
    if "excluded prior employer" in r:return "excluded_employer"
    if "sponsorship unavailable" in r:return "no_future_sponsorship"
    return "other_hard_filter"

def _norm_company(value):
    text=re.sub(r"[^a-z0-9]+"," ",(value or "").lower()).strip()
    # Normalize common legal suffixes/duplicate provider naming.
    text=re.sub(r"\b(incorporated|inc|corp|corporation|llc|ltd|limited|company|co)\b","",text)
    return re.sub(r"\s+"," ",text).strip()

def _norm_title(value):
    text=re.sub(r"\([^)]*\)"," ",(value or "").lower())
    text=re.sub(r"\b(remote|hybrid|on[- ]site|onsite)\b"," ",text)
    text=re.sub(r"[^a-z0-9]+"," ",text)
    return re.sub(r"\s+"," ",text).strip()

SOURCE_PRIORITY={name:0 for name in ALL_ATS_PROVIDERS}
SOURCE_PRIORITY.update({"career_site":1,"dice":2,"ziprecruiter":2})

def _dedup_eligible(items):
    """Prefer official ATS/company sources and use the persistent identity model."""
    kept=[];duplicates=[];seen={}
    items=sorted(items,key=lambda x: SOURCE_PRIORITY.get((x.get("job") or {}).get("source"),1))
    for item in items:
        raw=item["job"]
        keys=identity_keys(raw)
        duplicate=None
        for key in keys:
            if key in seen:
                duplicate=seen[key]
                break
        if duplicate:
            duplicates.append({"job":raw,"duplicate_of":duplicate["job"].get("external_id"),"action":"SKIP_DUPLICATE"})
            continue
        kept.append(item)
        for key in keys:
            seen[key]=item
    return kept,duplicates

def run(source_config,hours=24,only_source=None,dice_search_terms=None,ledger_path="generated/job_ledger.json",since=None,scan_now=None,source_since=None,source_hours=None,source_unit_hours=None):
    """Discover and eligibility-filter jobs only; no JD/resume score is used.

    `hours` is the incremental posting window. Use 24 for the first daily scan and
    1 for subsequent hourly scans; the persistent ledger prevents duplicate downstream work.
    """
    profile=load_profile();ledger=load_ledger(ledger_path)
    config=load_sources(source_config)
    # Company/career-site enrichment runs separately on a slower cadence.
    # Fast job cycles only consume persisted sources and learn newly seen employers.
    discovered_result=discover(config,only_source,dice_search_terms,hours=hours,source_hours=source_hours,source_unit_hours=source_unit_hours,return_coverage=True)
    if len(discovered_result)==3:
        jobs,errors,coverage=discovered_result
    else:
        jobs,errors=discovered_result
        coverage={}
    company_registry=load_company_registry()
    learn_companies_from_jobs(jobs,company_registry);save_company_registry(company_registry)
    source_since=source_since or {}
    source_hours=source_hours or {}
    source_unit_hours=source_unit_hours or {}
    # Record whether a posting was fetched through a source-recovery lookback.
    # This is diagnostic only: eligibility still uses the single production cutoff.
    for job in jobs:
        source=job.get("source")
        company=job.get("company_key") or job.get("company") or job.get("tenant")
        effective_hours=source_unit_hours.get(f"{source}:{company}",source_hours.get(source,hours))
        try: effective_hours=float(effective_hours)
        except Exception: effective_hours=float(hours)
        job["discovery_window_hours"]=effective_hours
        job["recovery_scan"]=effective_hours > float(hours) + 0.01
    # Provider/tenant recovery windows are discovery-only. A source may scan
    # farther back to repair missed coverage, but an old recovered posting must
    # never become a current-cycle candidate. Eligibility always uses the single
    # production cutoff supplied by the scheduler.
    jobs24,stale,already=fresh_jobs(jobs,hours,since=since,now=scan_now)
    eligible=[];skipped=[];reason_counts=Counter()
    for raw in jobs24:
        # Target companies are a preferred/example employer universe, not an
        # eligibility allowlist. Jobs from any employer continue through the same
        # freshness, DE-family, US-location, Full-Time/W-2, experience,
        # sponsorship, citizenship/clearance, ledger, JD and dedup checks.
        processed,key,prior=seen_or_submitted(raw,ledger)
        if processed:
            skipped.append({"job":raw,"reasons":["already processed in persistent ledger"],"action":"SKIP_ALREADY_PROCESSED"});reason_counts["already_processed_ledger"]+=1;continue
        record_seen(raw,ledger,"DISCOVERED")
        eligibility=two_category_filter(raw,profile);ok,reasons=passes_hard_filters(raw,profile)
        if not ok:
            skipped.append({"job":raw,"reasons":reasons,"eligibility":eligibility,"action":"SKIP"})
            for reason in reasons:reason_counts[_reason_key(reason)]+=1
            continue
        eligible.append({"job":raw,"eligibility":eligibility,"action":"ELIGIBLE_FOR_RESUME"})
    eligible,duplicates=_dedup_eligible(eligible)
    for item in eligible:record_seen(item["job"],ledger,"ELIGIBLE_FOR_RESUME")
    save_ledger(ledger,ledger_path)
    # Keep production diagnostics aligned with the exact provider universe that
    # discovery can execute, including newly learned long-tail ATS families.
    provider_names=tuple(dict.fromkeys(
        ALL_ATS_PROVIDERS+("career_site","dice","ziprecruiter","monster","hiringcafe","himalayas")
        +tuple(row.get("provider") for row in config.get("discovery_portal",[]) if row.get("enabled",True) and row.get("provider"))
    ))
    configured_sources={name for name in provider_names if (
        (config.get(name) and (not isinstance(config.get(name),dict) or config.get(name,{}).get("enabled",False)))
        or any(row.get("provider")==name and row.get("enabled",True) for row in config.get("discovery_portal",[]))
    )}
    failed_sources={e.get("source") for e in errors if e.get("source")}
    source_status={}
    for name in configured_sources:
        provider_errors=[e for e in errors if e.get("source")==name]
        if not provider_errors:
            source_status[name]="OK"
        elif isinstance(config.get(name),list):
            # Multi-unit providers are PARTIAL when at least one configured unit
            # remains healthy. Keep provider reporting consistent with discovery's
            # live summary (e.g. Eightfold Microsoft OK + Netflix failed).
            failed_units={e.get("company") for e in provider_errors if e.get("company")}
            configured_units={
                x.get("company") or x.get("tenant") or x.get("site") or x.get("board_token") or x.get("board_name")
                for x in config.get(name,[]) or []
            }
            configured_units.discard(None)
            source_status[name]="PARTIAL" if configured_units-failed_units else "ERROR"
        else:
            source_status[name]="ERROR"
    # Preserve provider-level failure details in the machine-readable report so
    # scheduler failures can be diagnosed without rerunning discovery manually.
    # Keep the original adapter error objects intact; they should already avoid
    # credentials/secrets and are more useful than a generic ERROR flag.
    source_errors={}
    for error in errors:
        source=error.get("source")
        if source:
            source_errors.setdefault(source,[]).append(error)
    # Workday is composed of independent company/tenant scans. Expose their
    # status separately so one broken tenant does not force every healthy tenant
    # to replay the same historical interval.
    # Preserve every executed employer/tenant unit, not only Workday. Discovery's
    # persisted source-health file is the authoritative execution ledger for this scan.
    source_unit_status={}
    try:
        health=json.loads(Path("state/source_health.json").read_text(encoding="utf-8"))
        for key,row in health.items():
            if not isinstance(row,dict): continue
            source=row.get("source"); company=row.get("company")
            if source and company:
                source_unit_status[f"{source}:{company}"]={
                    "source":source,"company":company,"status":row.get("status"),
                    "jobs_returned":row.get("jobs_returned",0),"error":row.get("error"),
                    "checked_at":row.get("checked_at")
                }
    except Exception:
        pass
    # Backfill explicit adapter errors even when tests or legacy callers do not
    # persist source-health, preserving backward-compatible Workday status strings.
    for error in errors:
        source=error.get("source"); company=error.get("company")
        if source and company and f"{source}:{company}" not in source_unit_status:
            source_unit_status[f"{source}:{company}"]="ERROR" if source=="workday" else {
                "source":source,"company":company,"status":"ERROR","jobs_returned":0,
                "error":error.get("error"),"checked_at":None
            }
    target_fresh=sum(bool(j.get("target_company")) for j in jobs24)
    target_eligible=sum(bool((x.get("job") or {}).get("target_company")) for x in eligible)
    target_rejected=sum(bool((x.get("job") or {}).get("target_company")) for x in skipped)
    diagnostics={
        "fresh_jobs_checked":len(jobs24),"target_company_jobs":target_fresh,"target_company_eligible":target_eligible,"target_company_rejected":target_rejected,
        "wrong_job_family":reason_counts["wrong_job_family"],
        "experience_mismatch":reason_counts["experience_mismatch"],
        "location_outside_us":reason_counts["location_outside_us"],
        "employment_type_mismatch":reason_counts["employment_type_mismatch"],
        "citizenship_required":reason_counts["citizenship_required"],
        "clearance_required":reason_counts["clearance_required"],
        "excluded_employer":reason_counts["excluded_employer"],
        "no_future_sponsorship":reason_counts["no_future_sponsorship"],
        "duplicates_removed":len(duplicates),"outside_target_company":reason_counts["outside_target_company"],
        "other_hard_filter":reason_counts["other_hard_filter"],
        "unique_hard_filter_rejections":sum(1 for x in skipped if x.get("action")=="SKIP"),
        "already_processed_ledger":reason_counts["already_processed_ledger"],"eligible_for_resume":len(eligible),
    }
    return {
        "discovered":len(jobs),"fresh_verified_within_hours":len(jobs24),"older_or_unverified":len(stale),"already_processed":len(already),
        "eligible":len(eligible),"target_company_jobs":target_fresh,"target_company_eligible":target_eligible,"target_company_rejected":target_rejected,"filtered_out":len(skipped)+len(duplicates),"filter_reason_counts":diagnostics,
        "action_counts":{"ELIGIBLE_FOR_RESUME":len(eligible),"SKIP":len(skipped),"SKIP_DUPLICATE":len(duplicates)},"errors":errors,"source_status":source_status,"source_errors":source_errors,"source_unit_status":source_unit_status,
        "coverage":coverage,"results":eligible,"hard_filter_rejections":skipped,"duplicate_rejections":duplicates,
    }

def _print_diagnostics(d,hours):
    print(f"\nLAST {hours} HOURS — ELIGIBILITY STAGE",flush=True)
    labels=[("Fresh verified jobs","fresh_jobs_checked"),("Wrong job family","wrong_job_family"),("Experience mismatch","experience_mismatch"),("Outside U.S.","location_outside_us"),("Employment type mismatch","employment_type_mismatch"),("Citizenship required","citizenship_required"),("Clearance required","clearance_required"),("Excluded prior employer","excluded_employer"),("No future sponsorship","no_future_sponsorship"),("Duplicates removed","duplicates_removed"),("Other eligibility filter","other_hard_filter"),("Unique hard-filter rejects","unique_hard_filter_rejections"),("Already processed ledger","already_processed_ledger"),("Eligible for resume","eligible_for_resume")]
    for label,key in labels:print(f"{label + ':':34} {d.get(key,0)}",flush=True)

def _print_eligible(results):
    print("\nELIGIBLE JOBS FOR RESUME STAGE",flush=True)
    if not results:print("None",flush=True);return
    for i,item in enumerate(results,1):
        raw=item["job"];elig=item["eligibility"];company=raw.get("company_key") or raw.get("company") or "Unknown"
        sponsorship=elig.get("sponsorship",{}).get("category") or "SPONSORSHIP_UNKNOWN"
        print(f"{i}. [{raw.get('source','?')}] {company} | {raw.get('title','')} | {raw.get('location') or 'Provider US-scoped / location not stated'}",flush=True)
        print(f"   sponsorship: {sponsorship}",flush=True)
        if raw.get("url"):print(f"   {raw['url']}",flush=True)

def _print_rejection_samples(items,limit=20):
    print("\nELIGIBILITY REJECTION SAMPLES",flush=True)
    if not items:print("None",flush=True);return
    for i,item in enumerate(items[:limit],1):
        raw=item["job"]
        print(f"{i}. [{raw.get('source','?')}] {raw.get('company_key') or 'Unknown'} | {raw.get('title','')} | {raw.get('location') or 'Location not stated'}",flush=True)
        print(f"   reasons: {'; '.join(item.get('reasons') or [])}",flush=True)

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--sources",default="data/job_sources.json");p.add_argument("--hours",type=int,default=24);p.add_argument("--output",help="Optional explicit output path. By default each standalone run gets a timestamped diagnostic file.");p.add_argument("--diagnostic-limit",type=int,default=20);p.add_argument("--only-source",choices=["greenhouse","lever","ashby","smartrecruiters","workday","dice","ziprecruiter","hiringcafe","himalayas","welcome_to_the_jungle","yc_jobs","discovery_portal"]);p.add_argument("--dice-term",action="append");p.add_argument("--ledger",default="generated/job_ledger.json");a=p.parse_args()
    report=run(a.sources,a.hours,a.only_source,a.dice_term,a.ledger);stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ");default_output=f"generated/diagnostics/{stamp}_{a.only_source or 'all'}_eligible.json";out=ROOT/(a.output or default_output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("results","hard_filter_rejections","duplicate_rejections")},indent=2));_print_diagnostics(report["filter_reason_counts"],a.hours);_print_eligible(report["results"]);_print_rejection_samples(report["hard_filter_rejections"],a.diagnostic_limit);print(f"\nSaved eligible jobs to {out}")
