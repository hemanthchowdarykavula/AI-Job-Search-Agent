from __future__ import annotations
import argparse, json
from datetime import datetime, timedelta
from pathlib import Path
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo=None
    ZoneInfoNotFoundError=Exception
from app.production_cycle import run_cycle
from app.application_queue import _application_gate
from app.config import load_profile
from app.job_ledger import load_ledger, save_ledger, record_seen, retry_metadata, _retry_due, _lookup
from app.source_registry import load_registry, as_discovery_config
from app.discovery import ALL_ATS_PROVIDERS

def _eastern_tz():
    """Use IANA Eastern time when available; fall back to Windows local Eastern time.

    Windows Python installations may not ship the IANA tz database. The fallback
    intentionally uses the machine's local timezone so DST remains correct when
    the Windows timezone is configured as Eastern Time.
    """
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            pass
    local=datetime.now().astimezone().tzinfo
    if local is None:
        raise RuntimeError("Unable to determine local timezone. Install tzdata or configure Windows timezone to Eastern Time.")
    return local

ET=_eastern_tz()
ROOT=Path(__file__).resolve().parent.parent
STATE_PATH=ROOT/"generated"/"scheduler_state.json"
BOOTSTRAP_HOUR=7
FINAL_HOUR=21
RUN_SLOTS=((7,30),(10,0),(12,30),(15,30),(18,30),(21,0))
SLOT_RECOVERY_MINUTES=55
INCREMENTAL_WINDOW_HOURS=2.5
RUN_WEEKDAYS={0,1,2,3,4}  # Monday-Friday

def _load_state():
    if not STATE_PATH.exists(): return {}
    try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception: return {}

def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
    STATE_PATH.write_text(json.dumps(state,indent=2),encoding="utf-8")

def _parse_state_time(value):
    if not value:return None
    try:return datetime.fromisoformat(value).astimezone(ET)
    except Exception:return None

def _unit_status_name(value):
    """Normalize legacy string and structured per-source status records."""
    if isinstance(value,dict):
        value=value.get("status")
    return str(value or "").strip().upper()

def _scheduled_cutoff(now,state):
    """Return the exact lower bound for this scan.

    The global production window starts at the most recent completed cycle,
    including a PARTIAL cycle. Provider-specific and Workday-tenant watermarks
    independently retain older cutoffs for sources that failed, so one unhealthy
    source cannot force every healthy provider to replay days of history. Legacy
    state without ``last_run_at`` falls back to ``last_successful_scan_at``.

    When there is no prior state, bootstrap from the previous weekday close so
    the first scheduled cycle still has a deterministic starting point.
    """
    last_run=_parse_state_time(state.get("last_run_at"))
    last_success=_parse_state_time(state.get("last_successful_scan_at"))
    candidates=[x for x in (last_run,last_success) if x is not None and x<=now+timedelta(minutes=10)]
    last=max(candidates) if candidates else None
    today=now.date()
    days_back=3 if now.weekday()==0 else 1
    prior_date=today-timedelta(days=days_back)
    prior_close=datetime(prior_date.year,prior_date.month,prior_date.day,FINAL_HOUR,tzinfo=ET)
    if last is None:return prior_close,"bootstrap"
    if last.date()!=today:return last,"bootstrap"
    return last,"incremental"

def _window_for(now,state):
    cutoff,mode=_scheduled_cutoff(now,state)
    seconds=max(1,(now-cutoff).total_seconds())
    return seconds/3600.0,mode,cutoff

def run_scheduled(sources="data/job_sources.json",ledger="generated/job_ledger.json",generate_resumes=True,limit=None,force=False,accepted_slot=None):
    now=datetime.now(ET)
    window_label="Monday-Friday 07:30,10:00,12:30,15:30,18:30,21:00 America/New_York"
    if not force and now.weekday() not in RUN_WEEKDAYS:
        return {"status":"OUTSIDE_RUN_WINDOW","local_time":now.isoformat(),"window":window_label}
    now_minutes=now.hour*60+now.minute
    active_slot=None
    accepted_slot_value=None
    if accepted_slot:
        try:
            slot_date,slot_clock=accepted_slot.split("T",1)
            slot_hour,slot_minute=(int(x) for x in slot_clock.split(":",1))
            if slot_date==now.date().isoformat() and (slot_hour,slot_minute) in RUN_SLOTS:
                active_slot=(slot_hour,slot_minute)
                accepted_slot_value=accepted_slot
        except (ValueError,TypeError):
            accepted_slot_value=None
    if active_slot is None:
        for hour,minute in RUN_SLOTS:
            slot_start=hour*60+minute
            if slot_start <= now_minutes < slot_start+SLOT_RECOVERY_MINUTES:
                active_slot=(hour,minute)
                break
    if not force and active_slot is None:
        return {"status":"OUTSIDE_RUN_WINDOW","local_time":now.isoformat(),"window":window_label}
    state=_load_state()
    # Cloudflare continues to provide repeated heartbeat opportunities. Once a
    # requested slot completes, later heartbeats inside its recovery window are no-ops.
    slot_hour,slot_minute=active_slot if active_slot is not None else (now.hour,now.minute)
    slot=accepted_slot_value or f"{now.date().isoformat()}T{slot_hour:02d}:{slot_minute:02d}"
    if not force and state.get("last_completed_slot")==slot:
        return {"status":"SLOT_ALREADY_COMPLETED","local_time":now.isoformat(),"slot":slot}
    hours,mode,cutoff=_window_for(now,state)
    # Each provider resumes from its own last successful discovery. Existing
    # scheduler state migrates safely by falling back to the global cutoff.
    source_config_for_watermarks=json.loads((ROOT/sources).read_text(encoding="utf-8")) if (ROOT/sources).exists() else {}
    portal_providers=tuple(row.get("provider") for row in source_config_for_watermarks.get("discovery_portal",[]) if isinstance(row,dict) and row.get("enabled",True) and row.get("provider"))
    providers=tuple(dict.fromkeys(ALL_ATS_PROVIDERS+("career_site","dice","ziprecruiter","monster","hiringcafe","himalayas")+portal_providers))
    watermarks=state.get("source_watermarks") or {}
    source_cutoffs={}
    source_hours={}
    for provider in providers:
        provider_cutoff=_parse_state_time(watermarks.get(provider)) or cutoff
        source_cutoffs[provider]=provider_cutoff.isoformat()
        source_hours[provider]=max(1,(now-provider_cutoff).total_seconds())/3600.0+(5.0/60.0)
    discovery_hours=hours+(5.0/60.0)
    # Workday tenants have independent failure domains. Preserve a watermark per
    # company so healthy tenants advance even when one tenant returns 5xx.
    try:
        source_config=json.loads((ROOT/sources).read_text(encoding="utf-8"))
    except Exception:
        source_config={}
    # Discovery merges learned Workday tenants from the source registry with the
    # static config. Build the scheduler's tenant list the same way; otherwise
    # learned tenants are scanned but never receive their own watermark and keep
    # replaying the provider-level historical catch-up window forever.
    configured_workday=list(source_config.get("workday",[]) or [])
    try:
        learned_workday=list(as_discovery_config(load_registry()).get("workday",[]) or [])
    except Exception:
        learned_workday=[]
    seen_workday={(x.get("host"),x.get("tenant"),x.get("site")) for x in configured_workday}
    for src in learned_workday:
        identity=(src.get("host"),src.get("tenant"),src.get("site"))
        if identity not in seen_workday:
            configured_workday.append(src);seen_workday.add(identity)
    unit_watermarks=state.get("source_unit_watermarks") or {}
    source_unit_hours={}
    for src in configured_workday:
        unit=src.get("company") or src.get("tenant")
        if not unit:continue
        key=f"workday:{unit}"
        unit_cutoff=_parse_state_time(unit_watermarks.get(key)) or _parse_state_time(watermarks.get("workday")) or cutoff
        source_unit_hours[key]=max(1,(now-unit_cutoff).total_seconds())/3600.0+(5.0/60.0)
    summary=run_cycle(sources=sources,hours=discovery_hours,ledger=ledger,generate_resumes=generate_resumes,limit=limit,
                      since=cutoff.isoformat(),scan_now=now,source_since=source_cutoffs,source_hours=source_hours,
                      source_unit_hours=source_unit_hours)

    # Resume generation and the application queue are the terminal automation boundary.
    # Applications are intentionally submitted manually by the user.
    summary["application_stage_enabled"]=False
    summary["applications_processed"]=0

    # Advance only providers that completed without discovery errors. A failed
    # provider keeps its old watermark and catches the missed interval next run.
    source_status=summary.get("source_status") or {}
    # Seed every provider with the cutoff actually used for this cycle. This
    # safely migrates legacy state that only has the global watermark: a failed
    # provider keeps the old cutoff instead of falling forward to the new global
    # success timestamp on the next run.
    next_watermarks={provider:source_cutoffs[provider] for provider in providers}
    next_watermarks.update(watermarks)
    for provider,status in source_status.items():
        if status=="OK":next_watermarks[provider]=now.isoformat()
    next_unit_watermarks=dict(unit_watermarks)
    unit_status=summary.get("source_unit_status") or {}
    # Every Workday tenant supplied to discovery completed unless it appears in
    # source_errors. Seed status for learned tenants too, because older reports
    # only enumerated the static config subset.
    failed_workday={e.get("company") for e in (summary.get("source_errors") or {}).get("workday",[]) if e.get("company")}
    for src in configured_workday:
        unit=src.get("company") or src.get("tenant")
        if unit:
            unit_status.setdefault(f"workday:{unit}","ERROR" if unit in failed_workday else "OK")
    for key,status in unit_status.items():
        if key not in next_unit_watermarks:
            next_unit_watermarks[key]=watermarks.get("workday") or source_cutoffs["workday"]
        if _unit_status_name(status)=="OK":
            next_unit_watermarks[key]=now.isoformat()
    # Keep the provider-level Workday watermark as the oldest tenant watermark.
    # This remains a conservative fallback for legacy code/state while actual
    # Workday network windows use the more precise per-tenant values above.
    workday_values=[_parse_state_time(v) for k,v in next_unit_watermarks.items() if k.startswith("workday:")]
    workday_values=[v for v in workday_values if v is not None]
    # If the whole Workday provider failed, keep its prior provider-level watermark.
    # Tenant watermarks may still exist from earlier successful runs, but they must
    # not make a failed provider appear to have advanced.
    if source_status.get("workday")!="ERROR" and workday_values:
        next_watermarks["workday"]=min(workday_values).isoformat()
    failed_providers=sorted(provider for provider,status in source_status.items() if status=="ERROR")
    state_update={"last_run_at":now.isoformat(),"last_completed_slot":slot,"last_mode":mode,"last_cycle_id":summary.get("cycle_id"),
                  "source_watermarks":next_watermarks,"source_unit_watermarks":next_unit_watermarks,
                  "last_cycle_status":"PARTIAL" if failed_providers else "SUCCESS",
                  "last_failed_providers":failed_providers}
    # Keep last_successful_scan_at reserved for a cycle where every reported
    # provider completed. Partial cycles are still slot-complete because failed
    # providers retain their own older watermarks and catch up next slot.
    if not failed_providers:
        state_update["last_successful_scan_at"]=now.isoformat()
    state.update(state_update)
    summary["scan_cutoff_local"]=cutoff.isoformat()
    summary["scan_window_hours"]=hours
    _save_state(state)
    summary["source_watermarks"]=next_watermarks
    summary["source_unit_watermarks"]=next_unit_watermarks
    summary["scheduler_mode"]=mode
    summary["scheduler_local_time"]=now.isoformat()
    summary["daily_final_cycle"]=active_slot==(21,0) if not force else now.hour==FINAL_HOUR
    summary["cycle_status"]="PARTIAL" if failed_providers else "SUCCESS"
    summary["failed_providers"]=failed_providers
    return summary

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--sources",default="data/job_sources.json")
    p.add_argument("--ledger",default="generated/job_ledger.json")
    p.add_argument("--no-resumes",action="store_true",help="Run discovery/finalization only.")
    p.add_argument("--limit",type=int)
    p.add_argument("--accepted-slot",help="Slot already accepted by the workflow guard, e.g. 2026-09-28T10:00.")
    p.add_argument("--force",action="store_true",help="Allow a manual test outside the scheduled 07:30-21:00 ET run slots.")
    a=p.parse_args()
    print(json.dumps(run_scheduled(a.sources,a.ledger,not a.no_resumes,a.limit,a.force),indent=2))