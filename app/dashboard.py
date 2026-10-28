from __future__ import annotations
import argparse, json, os, io, shutil, tarfile, tempfile, zipfile, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote
from app.job_identity import identity_keys
from app.manual_job_links import router as manual_job_links_router
from app.pasted_job_descriptions import router as pasted_jds_router
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

ROOT=Path(__file__).resolve().parents[1]
STATE_DIR=Path(os.getenv("JOB_AGENT_STATE_DIR", ROOT/"generated"))
LEDGER=STATE_DIR/"job_ledger.json"
CONFIRMED=STATE_DIR/"confirmed_applications.json"
HIDDEN=STATE_DIR/"hidden_applications.json"
CYCLES=STATE_DIR/"cycles"
app=FastAPI(title="AI Job Search Agent")
app.include_router(manual_job_links_router)
app.include_router(pasted_jds_router)

def _restore_resume_artifact_once():
    """Restore only resume files from a one-time trusted recovery artifact URL."""
    url=os.getenv("RESUME_RECOVERY_URL","").strip()
    if not url:return
    marker=STATE_DIR/".resume_recovery_complete"
    if marker.exists():return
    restored=0
    try:
        with urllib.request.urlopen(url,timeout=60) as response:
            payload=response.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                name=info.filename.replace("\\","/")
                if info.is_dir() or not name.startswith("generated/resumes/"):continue
                rel=Path(name).relative_to("generated")
                if rel.suffix.lower() not in {".pdf",".docx"}:continue
                dest=(STATE_DIR/rel).resolve()
                resumes_root=(STATE_DIR/"resumes").resolve()
                if resumes_root not in dest.parents:continue
                dest.parent.mkdir(parents=True,exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src,out)
                restored+=1
        marker.write_text(json.dumps({"restored_files":restored,"at":datetime.now(timezone.utc).isoformat()}),encoding="utf-8")
        print(f"Resume recovery restored {restored} files.")
    except Exception as exc:
        print(f"Resume recovery failed: {exc}")

@app.on_event("startup")
def _startup_resume_recovery():
    _restore_resume_artifact_once()

def _json(path,default):
    try:return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:return default

def _save_confirmed(data):
    CONFIRMED.parent.mkdir(parents=True,exist_ok=True)
    CONFIRMED.write_text(json.dumps(data,indent=2),encoding="utf-8")

def _hidden_keys():
    data=_json(HIDDEN,{"job_keys":[]})
    # v2 uses only exact job identities. Older hide state came from the broken
    # global/pipeline-scoped delete implementations and is intentionally reset
    # so historical dashboard rows are restored.
    if data.get("version") != 2:
        return set()
    return {str(key) for key in (data.get("job_keys") or [])}

def _hide_job(job_key):
    keys=_hidden_keys()
    keys.add(str(job_key))
    HIDDEN.parent.mkdir(parents=True,exist_ok=True)
    HIDDEN.write_text(json.dumps({"version":2,"job_keys":sorted(keys)},indent=2),encoding="utf-8")

def _delete_resume_artifacts(row,hist=None):
    """Delete generated resume artifacts for one dashboard application only."""
    candidates=[
        row.get("pdf_path"), row.get("resume_path"),
        (row.get("queue_item") or {}).get("resume_path"),
        (row.get("retry_application") or {}).get("resume_path"),
        (hist or {}).get("resume_path"),
    ]
    allowed_roots=[STATE_DIR.resolve(), (ROOT/"generated").resolve()]
    deleted=[]
    for value in candidates:
        if not value:continue
        p=Path(value)
        if not p.is_absolute():
            options=[STATE_DIR/p, ROOT/p, STATE_DIR.parent/p]
            p=next((x for x in options if x.exists()),options[0])
        elif not p.exists():
            parts=p.parts
            if "generated" in parts:
                idx=parts.index("generated")
                p=STATE_DIR/Path(*parts[idx+1:])
        for artifact in {p, p.with_suffix(".pdf"), p.with_suffix(".docx")}:
            try:
                resolved=artifact.resolve()
                if artifact.exists() and artifact.is_file() and any(root==resolved or root in resolved.parents for root in allowed_roots):
                    artifact.unlink()
                    deleted.append(str(artifact))
            except (OSError,ValueError):
                continue
    return deleted

def _resume_path(row):
    candidates=[
        row.get("pdf_path"),
        (row.get("queue_item") or {}).get("resume_path"),
        (row.get("retry_application") or {}).get("resume_path"),
        row.get("resume_path"),
    ]
    for value in candidates:
        if value:
            p=Path(value)
            if not p.is_absolute():
                local=ROOT/p
                persisted=STATE_DIR.parent/p
                p=persisted if persisted.exists() else local
            elif not p.exists():
                # Queue entries created on ephemeral GitHub runners can contain
                # absolute /home/runner/.../generated/... paths. After syncing to
                # Railway, remap that generated-relative suffix onto STATE_DIR.
                parts=p.parts
                try:
                    generated_idx=parts.index("generated")
                except ValueError:
                    generated_idx=-1
                if generated_idx >= 0:
                    relative=Path(*parts[generated_idx+1:])
                    persisted=STATE_DIR/relative
                    if persisted.exists():
                        p=persisted
            if p.suffix.lower()==".pdf" and p.exists() and p.is_file():return p
    return None

def _portal(row):
    source=(row.get("source") or "").lower();url=row.get("url") or ""
    if "dice" in source or "dice.com" in url:return "Dice"
    if "workday" in source or "myworkdayjobs" in url:return "Workday"
    if "greenhouse" in source or "greenhouse" in url:return "Greenhouse"
    if "lever" in source or "lever.co" in url:return "Lever"
    if "smartrecruiters" in source:return "SmartRecruiters"
    if "ashby" in source:return "Ashby"
    if "ziprecruiter" in source:return "ZipRecruiter"
    return row.get("source") or "Company site"

def _stage(status):
    s=status or "DISCOVERED"
    if s in {"SUBMITTED","SUBMITTED_CONFIRMED"}:return "Applied"
    if s=="SUBMISSION_ATTEMPTED":return "Verify submission"
    if s=="READY_TO_APPLY":return "Ready to apply"
    if s=="MANUAL_READY_TO_APPLY":return "Manual apply"
    if s in {"APPLICATION_IN_PROGRESS","IN_PROGRESS"}:return "Applying"
    if s in {"RETRY_APPLICATION","RETRY_RESUME_GENERATION"}:return "Retrying"
    if s in {"MANUAL_ACTION_REQUIRED","SECURITY_BLOCKED"}:return "Needs attention"
    if s in {"PERMANENT_SKIP","HOLD_ATS_REVIEW","HOLD_ARTIFACT_VALIDATION"}:return "Not applying"
    if "RESUME" in s:return "Resume"
    return "Processing"

def _confirmed_map():
    data=_json(CONFIRMED,{"applications":[]})
    apps=data.get("applications") or []
    return data,apps

def _resume_path_from_confirmed(hist):
    if not hist:return None
    value=hist.get("resume_path")
    if value:
        p=Path(value)
        if not p.is_absolute():
            p=STATE_DIR/p
        if p.suffix.lower()==".pdf" and p.exists() and p.is_file():
            return p
    # Older applied records did not persist the resume path. Recover the
    # validated artifact from another ledger record for the same company/title.
    company=str(hist.get("company") or "").strip().lower()
    title=str(hist.get("title") or "").strip().lower()
    if not company or not title:return None
    ledger=_json(LEDGER,{"jobs":{}})
    for row in (ledger.get("jobs") or {}).values():
        if str(row.get("company") or "").strip().lower()!=company:continue
        if str(row.get("title") or "").strip().lower()!=title:continue
        p=_resume_path(row)
        if p:return p
    return None

def _pipeline_runs():
    runs=[]
    if CYCLES.exists():
        for p in CYCLES.glob("*_summary.json"):
            d=_json(p,{})
            cid=str(d.get("cycle_id") or p.name.replace("_summary.json",""))
            try: ts=datetime.strptime(cid,"%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except Exception: continue
            unit_map=d.get("source_unit_status") or {}
            unit_rows=unit_map.values()
            unit_statuses=[(x.get("status") if isinstance(x,dict) else x) for x in unit_rows]
            source_issues=[]
            for key,row in unit_map.items():
                if isinstance(row,dict):
                    status=row.get("status")
                    source=row.get("source") or str(key).partition(":")[0]
                    company=row.get("company") or str(key).partition(":")[2]
                    error=row.get("error") or row.get("health_status")
                    next_retry=row.get("next_retry_at")
                else:
                    status=row;source=str(key).partition(":")[0];company=str(key).partition(":")[2];error=None;next_retry=None
                if status and status!="OK":
                    source_issues.append({"source":source,"company":company,"status":status,"error":error,"next_retry_at":next_retry})
            source_issues.sort(key=lambda x:(x.get("status")!="ERROR",x.get("source") or "",x.get("company") or ""))
            repair=d.get("source_repair_queue") or {}
            reasons=d.get("filter_reason_counts") or {}
            audit=_json(CYCLES/f"{cid}_summary_acceptance.json",{})
            runs.append({
                "cycle_id":cid,"ts":ts,"created":ts.isoformat(),
                "production_cutoff":d.get("production_cutoff"),
                "discovered":d.get("discovered",0),
                "fresh":d.get("fresh_verified_within_window",0),
                "older_or_unverified":d.get("older_or_unverified",0),
                "filtered":d.get("filtered_out",0),
                "preliminary":d.get("preliminary_eligible",0),
                "final_verified":d.get("final_jd_verified",0),
                "held_or_rejected":d.get("held_or_rejected",0),
                "eligible":d.get("eligible",0),
                "prepared":d.get("prepared",0),
                "ready":d.get("ready_to_apply",0),
                "filter_reasons":reasons,
                "source_total":len(unit_statuses),
                "source_ok":sum(x=="OK" for x in unit_statuses),
                "source_failed":sum(x=="ERROR" for x in unit_statuses),
                "source_backoff":sum(x=="BACKOFF_TRANSIENT" for x in unit_statuses),
                "source_hard_skipped":sum(x in {"SKIPPED_UNHEALTHY","SKIPPED_HARD_FAILURE"} for x in unit_statuses),
                "repair_pending":repair.get("pending",0),
                "repair_failed":repair.get("failed",0),
                "repair_repaired":repair.get("repaired",0),
                "source_issue_count":len(source_issues),
                "source_issues":source_issues[:25],
                "audit_passed":audit.get("passed") if audit else None,
                "audit_failures":audit.get("failures") or [],
            })
    runs.sort(key=lambda x:x["ts"])
    return runs

def _pipeline_for(value,runs):
    if not value:return None
    try:
        t=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if t.tzinfo is None:t=t.replace(tzinfo=timezone.utc)
    except Exception:return None
    prior=[r for r in runs if r["ts"]<=t]
    if not prior:return None
    r=prior[-1]
    return r["cycle_id"] if (t-r["ts"]).total_seconds()<=4*3600 else None

def _cycle_snapshot(cycle_id):
    """Return the immutable application-ready rows produced by one pipeline run."""
    if not cycle_id or not CYCLES.exists(): return []
    # The summary's ready_to_apply count is calculated from the manifest, so
    # View Jobs must use that same manifest as its source of truth.
    manifest=_json(CYCLES/f"{cycle_id}_manifest.json",[])
    rows=manifest if isinstance(manifest,list) else (manifest.get("results") or manifest.get("jobs") or manifest.get("applications") or [])
    if rows:
        ready=[]
        seen=set()
        for row in rows:
            if not isinstance(row,dict) or row.get("next_action") not in {"READY_TO_APPLY","MANUAL_READY_TO_APPLY"}:continue
            keys=identity_keys(row)
            if any(key in seen for key in keys):continue
            seen.update(keys)
            ready.append(row)
        return ready
    # Older cycles may only have an application queue.
    queue=_json(CYCLES/f"{cycle_id}_application_queue.json",[])
    rows=queue if isinstance(queue,list) else (queue.get("applications") or queue.get("jobs") or queue.get("queue") or [])
    return [x for x in rows if isinstance(x,dict)]


def _pipeline_jobs(cycle_id):
    snapshot=_cycle_snapshot(cycle_id)
    hidden=_hidden_keys()
    current=_jobs()
    by_key={str(x.get("key") or ""):x for x in current if x.get("key")}
    out=[]
    for row in snapshot:
        key=str(row.get("job_key") or row.get("key") or row.get("external_id") or row.get("job_id") or "")
        if key and key in hidden:continue
        live=by_key.get(key)
        if not live:
            company=str(row.get("company") or row.get("company_name") or "").strip().lower()
            title=str(row.get("title") or row.get("job_title") or "").strip().lower()
            if company and title:
                live=next((x for x in current if str(x.get("company") or "").strip().lower()==company and str(x.get("title") or "").strip().lower()==title),None)
        if live:
            item=dict(live)
        else:
            rp=_resume_path(row)
            if not rp and key:
                ledger_row=(_json(LEDGER,{"jobs":{}}).get("jobs") or {}).get(key)
                if ledger_row:
                    rp=_resume_path(ledger_row)
            item={
                "key":key or f"{cycle_id}:{len(out)}",
                "company":row.get("company") or row.get("company_name") or "",
                "title":row.get("title") or row.get("job_title") or "",
                "location":row.get("location") or "",
                "portal":row.get("portal") or row.get("source") or "",
                "url":row.get("url") or row.get("job_url") or row.get("apply_url") or "",
                "created":row.get("created") or row.get("first_seen") or row.get("created_at") or "",
                "stage":"Ready to apply",
                "status":row.get("next_action") or "READY_TO_APPLY",
                "resume":rp.name if rp else None,
                "resume_url":"/resume/"+quote(key,safe="") if rp else None,
                "resume_available":bool(rp),
                "freshness_proof":row.get("freshness_proof"),
                "official_posted_at":row.get("official_posted_at"),
                "official_posted_label":row.get("official_posted_label"),
                "freshness_basis":row.get("freshness_basis"),
                "recovery_scan":bool(row.get("recovery_scan")),
                "live_check":row.get("live_check"),
            }
        item["pipeline"]=cycle_id
        out.append(item)
    return out


def _jobs():
    ledger=_json(LEDGER,{"jobs":{}})
    _,confirmed=_confirmed_map()
    by_key={x.get("job_key"):x for x in confirmed if x.get("job_key")}
    by_name={(str(x.get("company") or "").lower(),str(x.get("title") or "").lower()):x for x in confirmed}
    out=[]
    runs=_pipeline_runs()
    hidden=_hidden_keys()
    active={"READY_TO_APPLY","APPLICATION_IN_PROGRESS","IN_PROGRESS","RETRY_APPLICATION","RETRY_RESUME_GENERATION","SUBMISSION_ATTEMPTED","SUBMITTED","SUBMITTED_CONFIRMED","MANUAL_ACTION_REQUIRED","SECURITY_BLOCKED"}
    for key,row in (ledger.get("jobs") or {}).items():
        if key in hidden:continue
        hist=by_key.get(key)
        status=(hist or {}).get("status") or row.get("application_status") or "DISCOVERED"
        if status not in active:continue
        rp=_resume_path(row) or _resume_path_from_confirmed(hist)
        out.append({
            "key":key,
            "company":row.get("company") or (hist or {}).get("company") or "Unknown company",
            "title":row.get("title") or (hist or {}).get("title") or "Unknown role",
            "status":status,
            "stage":_stage(status),
            "source":row.get("source") or "",
            "portal":_portal(row),
            "url":row.get("url") or (row.get("queue_item") or {}).get("url") or "",
            "resume":rp.name if rp else None,
            "resume_url":"/resume/"+quote(key,safe="") if rp else None,
            "updated":row.get("last_seen") or row.get("first_seen"),
            "created":row.get("first_seen") or row.get("last_seen"),
            "location":row.get("location") or "",
            "pipeline":row.get("cycle_id") or _pipeline_for(row.get("first_seen") or row.get("last_seen"),runs),
            "applied_at":(hist or {}).get("submitted_at") or row.get("submitted_at"),
            "reason":(hist or {}).get("reason") or row.get("application_reason") or "",
            "freshness_proof":row.get("freshness_proof") or (row.get("queue_item") or {}).get("freshness_proof"),
            "official_posted_at":row.get("official_posted_at") or (row.get("queue_item") or {}).get("official_posted_at"),
            "official_posted_label":row.get("official_posted_label") or (row.get("queue_item") or {}).get("official_posted_label"),
            "freshness_basis":row.get("freshness_basis") or (row.get("queue_item") or {}).get("freshness_basis"),
            "recovery_scan":bool(row.get("recovery_scan") or (row.get("queue_item") or {}).get("recovery_scan")),
            "live_check":row.get("live_check") or (row.get("queue_item") or {}).get("live_check"),
        })
    out.sort(key=lambda x:x.get("updated") or x.get("applied_at") or "",reverse=True)
    return out

@app.get("/health")
def health():
    return {"ok":True,"ledger_exists":LEDGER.exists(),"confirmed_exists":CONFIRMED.exists()}

@app.post("/api/recover-resumes")
async def recover_resumes(request:Request):
    expected=os.getenv("DASHBOARD_SYNC_TOKEN","")
    supplied=request.headers.get("x-dashboard-token","")
    if not expected or supplied != expected:
        raise HTTPException(401,"Unauthorized")
    body=await request.body()
    if not body:
        raise HTTPException(400,"Empty recovery archive")
    restored=0
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for info in zf.infolist():
                name=info.filename.replace("\\","/")
                if info.is_dir() or not name.startswith("generated/resumes/"):
                    continue
                rel=Path(name).relative_to("generated")
                if rel.suffix.lower() not in {".pdf",".docx"}:
                    continue
                dest=(STATE_DIR/rel).resolve()
                root=(STATE_DIR/"resumes").resolve()
                if root not in dest.parents:
                    continue
                dest.parent.mkdir(parents=True,exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src,out)
                restored+=1
    except zipfile.BadZipFile:
        raise HTTPException(400,"Invalid ZIP archive")
    return {"ok":True,"restored_files":restored}

@app.post("/api/sync")
async def sync_state(request:Request):
    expected=os.getenv("DASHBOARD_SYNC_TOKEN","")
    supplied=request.headers.get("x-dashboard-token","")
    if not expected or supplied != expected:
        raise HTTPException(401,"Unauthorized")
    body=await request.body()
    if not body:
        raise HTTPException(400,"Empty sync payload")
    with tempfile.TemporaryDirectory() as td:
        tmp=Path(td)
        try:
            with tarfile.open(fileobj=io.BytesIO(body),mode="r:gz") as tf:
                for member in tf.getmembers():
                    target=(tmp/member.name).resolve()
                    if tmp.resolve() not in target.parents and target != tmp.resolve():
                        raise HTTPException(400,"Unsafe archive path")
                tf.extractall(tmp)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400,f"Invalid sync archive: {exc}")
        source=tmp/"generated"
        if not source.exists():
            raise HTTPException(400,"Archive must contain generated/")
        STATE_DIR.mkdir(parents=True,exist_ok=True)
        for item in source.iterdir():
            if item.name in {"confirmed_applications.json","hidden_applications.json"}:
                continue
            dest=STATE_DIR/item.name
            if item.is_dir():
                shutil.copytree(item,dest,dirs_exist_ok=True)
            else:
                shutil.copy2(item,dest)
    return {"ok":True,"ledger_exists":LEDGER.exists()}

@app.get("/api/pipelines")
def pipelines():
    return {"pipelines":[{k:v for k,v in r.items() if k!="ts"} for r in reversed(_pipeline_runs())]}

@app.get("/api/applications")
def applications(pipeline: str | None = None, date: str | None = None):
    if pipeline:
        rows=_pipeline_jobs(pipeline)
    elif date:
        day_runs=[run for run in _pipeline_runs() if run["ts"].astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d") == date]
        if len(day_runs) == 1:
            # A cycle snapshot is already identity-deduplicated. Preserve it exactly.
            rows=_pipeline_jobs(day_runs[0]["cycle_id"])
        else:
            rows=[]; seen=set()
            for run in day_runs:
                for row in _pipeline_jobs(run["cycle_id"]):
                    keys=identity_keys(row)
                    if any(key in seen for key in keys): continue
                    seen.update(keys); rows.append(row)
    else:
        rows=_jobs()
    return {"applications":rows,"counts":{
        "all":len(rows),
        "queue":sum(x["stage"] in {"Ready to apply","Applying"} for x in rows),
        "applied":sum(x["stage"]=="Applied" for x in rows),
        "attention":sum(x["stage"] in {"Needs attention","Verify submission"} for x in rows),
    }}

@app.delete("/api/applications/{job_key:path}")
def delete_application(job_key:str):
    ledger=_json(LEDGER,{"jobs":{}})
    row=(ledger.get("jobs") or {}).get(job_key)
    # Historical rows may no longer exist in the current ledger. Resolve only
    # the exact immutable job key so one click can never widen to another row.
    if not row and CYCLES.exists():
        for manifest_path in sorted(CYCLES.glob("*_manifest.json"),reverse=True):
            manifest=_json(manifest_path,[])
            rows=manifest if isinstance(manifest,list) else (manifest.get("results") or manifest.get("jobs") or manifest.get("applications") or [])
            row=next((x for x in rows if isinstance(x,dict) and str(x.get("job_key") or x.get("key") or x.get("external_id") or x.get("job_id") or "")==job_key),None)
            if row:break
    row=row or {}

    # One click deletes exactly one application identity. Never infer related
    # records by company/title and never widen deletion to a date or pipeline.
    data,apps=_confirmed_map()
    exact=[x for x in apps if x.get("job_key")==job_key]
    # Dashboard deletion is logical, not physical. Historical rows can refer to
    # preserved resume artifacts through legacy/shared paths, so unlinking a
    # file here can break View Resume for unrelated applications.
    deleted_files=[]
    data["applications"]=[x for x in apps if x.get("job_key")!=job_key]
    if len(data["applications"])!=len(apps):
        _save_confirmed(data)
    _hide_job(job_key)
    return {"ok":True,"deleted":True,"job_key":job_key,"deleted_files":deleted_files}

@app.post("/api/applications/{job_key:path}/confirm-submitted")
def confirm_submitted(job_key:str):
    ledger=_json(LEDGER,{"jobs":{}})
    row=(ledger.get("jobs") or {}).get(job_key)
    if not row:raise HTTPException(404,"Job not found")
    now=datetime.now(timezone.utc).isoformat()
    data,apps=_confirmed_map()
    existing=next((x for x in apps if x.get("job_key")==job_key),None)
    rp=_resume_path(row)
    payload={
        "job_key":job_key,
        "company":row.get("company"),
        "title":row.get("title"),
        "url":row.get("url") or (row.get("queue_item") or {}).get("url") or "",
        "resume_path":str(rp.relative_to(STATE_DIR)) if rp and STATE_DIR in rp.parents else (str(rp) if rp else None),
        "status":"SUBMITTED_CONFIRMED",
        "submitted_at":now,
        "reason":"Marked applied from hosted dashboard.",
    }
    if existing:existing.update(payload)
    else:apps.append(payload)
    data["applications"]=apps
    _save_confirmed(data)
    return {"ok":True,"status":"SUBMITTED_CONFIRMED","submitted_at":now}

@app.get("/resume/{job_key:path}")
def resume(job_key:str):
    ledger=_json(LEDGER,{"jobs":{}})
    row=(ledger.get("jobs") or {}).get(job_key)
    # Historical dashboard rows can outlive their current-ledger entry. Resolve
    # those rows only by the exact immutable job identity; never by company/title.
    if not row and CYCLES.exists():
        for manifest_path in sorted(CYCLES.glob("*_manifest.json"),reverse=True):
            manifest=_json(manifest_path,[])
            rows=manifest if isinstance(manifest,list) else (manifest.get("results") or manifest.get("jobs") or manifest.get("applications") or [])
            row=next((x for x in rows if isinstance(x,dict) and str(x.get("job_key") or x.get("key") or x.get("external_id") or x.get("job_id") or "")==job_key),None)
            if row:break
    if not row:raise HTTPException(404,"Job not found")
    _,confirmed=_confirmed_map()
    hist=next((x for x in confirmed if x.get("job_key")==job_key),None)
    p=_resume_path(row) or _resume_path_from_confirmed(hist)
    if not p:raise HTTPException(404,"Resume not available")
    return FileResponse(p,media_type="application/pdf",headers={"Content-Disposition":f'inline; filename="{p.name}"'})

@app.get("/",response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Auto Apply</title><style>
:root{--bg:#07111f;--panel:#0c1828;--panel2:#0f1d2e;--line:#21344a;--line2:#1a2a3d;--text:#edf3fb;--muted:#91a4bc;--blue:#3d7bea;--green:#55dea5}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14px}.app{display:grid;grid-template-columns:210px minmax(0,1fr);min-height:100vh}.side{background:#0a1625;border-right:1px solid var(--line2);padding:22px 14px;position:sticky;top:0;height:100vh}.brand{font-size:20px;font-weight:850;padding:0 8px 24px}.brand small{display:block;color:var(--muted);font-size:11px;font-weight:500;margin-top:4px}.nav{display:grid;gap:6px}.nav a{display:block;padding:11px 12px;border-radius:8px;color:#c7d2e2;font-size:13px;text-decoration:none}.nav a:hover,.nav .active{background:#173967;color:#fff}.sched{display:none}.main{padding:22px clamp(16px,2vw,30px) 36px;width:100%;min-width:0;max-width:none}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.top h1{font-size:23px;margin:0 0 4px}.muted{color:var(--muted);font-size:12px}.datepick{background:#0d1a2b;border:1px solid #263950;color:#e7eef8;border-radius:8px;padding:10px 13px}.runBtn{background:#1677e8;border:1px solid #2684ff;color:#fff;border-radius:8px;padding:10px 14px;text-decoration:none;font-size:12px;font-weight:750}.miniLink{display:inline-block;border:1px solid #2a4058;border-radius:7px;padding:6px 9px;color:#dce7f5;text-decoration:none}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}.stat{background:#0d1a2b;border:1px solid #263950;border-radius:10px;padding:15px 16px}.stat span{color:#a9b8ca;font-size:11px}.stat b{display:block;font-size:24px;margin-top:5px}.tabsbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:16px}.tabs{display:flex;gap:7px;overflow:auto;min-width:0}.tab{border:1px solid #263950;background:#0d1a2b;color:#aebdd0;border-radius:8px;padding:9px 13px;white-space:nowrap;cursor:pointer}.tab.active{background:#2684ff;color:#fff;border-color:#2684ff}.filters{display:flex;gap:7px;flex:0 0 auto}.filters input,.filters select{background:#0d1a2b;border:1px solid #263950;color:#dce6f4;border-radius:8px;padding:9px 11px;min-width:0}.section{background:#0b1726;border:1px solid var(--line);border-radius:11px;margin-bottom:16px;overflow:visible}.sectionHead{padding:14px 16px;border-bottom:1px solid #1d3044;font-weight:800;display:flex;justify-content:space-between;align-items:center}.pipes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:12px}.pipe{border:1px solid #263950;background:#0e1c2e;border-radius:9px;padding:13px}.pipe b{font-size:13px}.pipe .green{color:#42dda0}.pipe div{margin-top:6px;color:#9fb0c7;font-size:11px}
.row{display:grid;grid-template-columns:minmax(260px,1fr) 130px 180px minmax(340px,380px);column-gap:18px;align-items:center;padding:14px 16px;border-bottom:1px solid #17283a;font-size:12px}.row:last-child{border-bottom:0}.row:not(.head):hover{background:#0e1c2d}.head{background:#101f31;color:#9fb0c7;font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding-top:12px;padding-bottom:12px}.jobInfo,.statusCell,.row>div{min-width:0}.title{font-weight:800;color:#f3f7fc;font-size:13px;line-height:1.3;margin-bottom:6px;overflow-wrap:anywhere}.meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:#8fa0b8;font-size:11px}.meta .company{color:#b9c8da;font-weight:700}.metaDot{color:#40536b}.company{color:#b5c3d4}.created{color:#a9b8ca;line-height:1.35;white-space:nowrap}.badge{display:inline-flex;align-items:center;justify-content:center;padding:5px 9px;border-radius:999px;font-size:10px;font-weight:800;background:#17304d;white-space:nowrap}.ready-to-apply,.manual-apply,.applying{background:#0d4637;color:#62e5b0}.applied{background:#173b69;color:#74b4ff}.needs-attention,.verify-submission{background:#522d31;color:#ff8f91}.retrying{background:#392d61;color:#c6a8ff}
.actions{display:grid;grid-template-columns:82px 94px 106px 36px;gap:6px;align-items:center;justify-content:end;width:100%}.btn{border:1px solid #30465f;background:#13253a;color:#dce7f5;text-decoration:none;padding:7px 8px;border-radius:7px;font-size:10px;font-weight:700;cursor:pointer;white-space:nowrap;text-align:center;min-height:32px}.actions>.btn,.actions>.badge{height:32px;display:flex;align-items:center;justify-content:center;padding:0 8px}.actions .primary{min-width:0}.actions .applied{margin:0}.btn.primary{background:#2f73df;border-color:#4388f4;color:#fff}.menuWrap{position:relative;width:36px}.actions .menuWrap>.btn{width:36px;height:32px;padding:0}.menu{display:none;position:absolute;right:0;top:37px;background:#102033;border:1px solid #2a4058;padding:6px;border-radius:8px;z-index:20;box-shadow:0 10px 30px #0008}.menu.open{display:block}.deleteBtn{color:#ff8f91}.empty{padding:26px;text-align:center;color:#8192a8}.count{color:#8fa0b8;font-size:11px}
@media(max-width:1200px){.app{grid-template-columns:180px minmax(0,1fr)}.row{grid-template-columns:minmax(220px,1fr) 120px 155px 330px;column-gap:12px}.actions{grid-template-columns:76px 88px 100px 34px;gap:5px}.actions .menuWrap>.btn,.menuWrap{width:34px}.btn{font-size:9.5px}}
@media(max-width:980px){.app{grid-template-columns:1fr}.side{position:relative;width:100%;height:auto;padding:11px 14px;border-right:0;border-bottom:1px solid #1c2a3c}.brand{padding:0 4px 10px;font-size:18px}.nav{display:flex;gap:6px;overflow-x:auto}.nav a{flex:0 0 auto;padding:8px 10px}.main{padding:16px}.stats{grid-template-columns:repeat(2,1fr)}.pipes{grid-template-columns:1fr}.tabsbar{align-items:stretch;flex-direction:column}.filters{width:100%}.filters>*{flex:1}.row{grid-template-columns:minmax(200px,1fr) 120px 150px}.head>div:last-child{display:none}.row>div:last-child{grid-column:1/-1}.actions{justify-content:start;width:auto;grid-template-columns:82px 94px 106px 36px;margin-top:2px}}
@media(max-width:650px){.main{padding:10px}.top{align-items:flex-start;gap:10px;margin-bottom:14px}.top h1{font-size:20px}.datepick{padding:7px 9px;font-size:10px}.stats{gap:7px;margin-bottom:12px}.stat{padding:11px}.stat b{font-size:20px}.filters{display:grid;grid-template-columns:1fr}.section{border-radius:8px}.sectionHead{padding:11px}.pipes{padding:9px;gap:9px}.row{display:block;padding:13px}.head{display:none}.row>div{margin-bottom:7px}.row>div:last-child{margin-bottom:0}.title{font-size:14px}.actions{margin-top:9px;grid-template-columns:repeat(2,minmax(0,1fr));width:100%}.actions .menuWrap{width:100%}.actions .menuWrap>.btn{width:100%}.actions>.btn,.actions>.badge,.actions .menuWrap>.btn{height:36px}.menu{left:0;right:auto;top:40px}.tabs{width:100%}.tab{padding:8px 11px}.count{font-size:10px}}
</style></head><body><div class="app"><aside class="side"><div class="brand">💼 Auto Apply<small>Job Application Manager</small></div><div class="nav"><a class="active" href="/">⌂ &nbsp; Job Discovery</a><a href="/manual-links">🔗 &nbsp; Manual Job Links</a><a href="/pasted-jds">📝 &nbsp; Paste Job Description</a></div></aside><main class="main" id="dashboard"><div class="top"><div><h1>👋 Good Morning!</h1><div class="muted">Here's your job application overview.</div></div><div class="datepick" id="todayLabel"></div></div>
<div class="stats"><div class="stat"><span>Total Applications</span><b id="all">0</b></div><div class="stat"><span>Ready to Apply</span><b id="queue">0</b></div><div class="stat"><span>Applied</span><b id="applied">0</b></div><div class="stat"><span>Needs Attention</span><b id="attention">0</b></div></div>
<div class="tabsbar"><div class="tabs" id="tabs"></div><div class="filters"><input id="datePicker" type="date" aria-label="Select date"><input id="search" placeholder="⌕ Search jobs, companies..."><select id="filter"><option value="">All statuses</option><option>Ready to apply</option><option>Manual apply</option><option>Applied</option><option>Applying</option><option>Retrying</option><option>Needs attention</option><option>Verify submission</option></select></div></div>
<section class="section" id="pipelinesSection"><div class="sectionHead"><span id="pipeTitle">Pipeline Runs</span><span class="count"><span id="pipeCount"></span> &nbsp; <a class="miniLink" href="#pipelinesSection">View All Pipelines</a></span></div><div class="pipes" id="pipes"></div></section>
<section class="section" id="jobsSection"><div class="sectionHead"><span id="jobsTitle">Jobs</span><span class="count" id="jobCount"></span></div><div class="row head"><div>Job</div><div>Status</div><div>Applied Date</div><div>Actions</div></div><div id="jobs"></div></section></main></div><script>
let rows=[],pipelineRows=[],selectedDate=null,selectedPipeline="";const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));const cls=s=>String(s).toLowerCase().replaceAll(" ","-");const dateKey=v=>{if(!v)return"";let d=new Date(v);if(isNaN(d))return String(v).slice(0,10);let parts=new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(d),o=Object.fromEntries(parts.map(x=>[x.type,x.value]));return o.year+"-"+o.month+"-"+o.day};const niceDate=v=>{if(!v)return"—";let d=new Date(v);return isNaN(d)?v:d.toLocaleString([],{month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"})};const labelDate=k=>{let d=new Date(k+"T12:00:00");return d.toLocaleDateString([],{month:"short",day:"numeric"})};const pipelineName=id=>id?("Run "+id.slice(9,13)):"—";const postedLabel=r=>{let p=r.official_posted_label||(r.freshness_proof||{}).label;if(p)return p;let v=r.official_posted_at||(r.freshness_proof||{}).posted_at;return v?niceDate(v):"Posting date verified"};const sourceIssueText=r=>{let x=r.source_issues||[];if(!x.length)return"No source issues";let names=x.slice(0,6).map(v=>(v.source||"?")+": "+(v.company||"?")+" ["+(v.status||"?")+"]");return names.join(" · ")+(r.source_issue_count>x.length?" · …":"")};const reasonSummary=r=>{let x=r.filter_reasons||{},v=[];if(x.wrong_job_family)v.push(x.wrong_job_family+" wrong family");if(x.experience_mismatch)v.push(x.experience_mismatch+" experience");if(x.other_hard_filter)v.push(x.other_hard_filter+" other");if(x.already_processed_ledger)v.push(x.already_processed_ledger+" processed");return v.join(" · ")||"No filter rejections"};
function counts(x){all.textContent=x.length;queue.textContent=x.filter(r=>["Ready to apply","Applying"].includes(r.stage)).length;applied.textContent=x.filter(r=>r.stage==="Applied").length;attention.textContent=x.filter(r=>["Needs attention","Verify submission"].includes(r.stage)).length}
function renderTabs(){let today=dateKey(new Date());if(selectedDate===null)selectedDate=today;tabs.innerHTML='<button class="tab '+(selectedDate===today?"active":"")+'" data-date="'+today+'">Today</button><button class="tab '+(!selectedDate?"active":"")+'" data-all-dates="1">All Dates</button><button class="tab" data-prev="1">← Previous</button><button class="tab" data-next="1">Next →</button>'}
function render(){datePicker.value=selectedDate||"";let q=search.value.toLowerCase(),f=filter.value;let x=rows.filter(r=>(!selectedPipeline||r.pipeline===selectedPipeline)&&(!q||(r.company+" "+r.title+" "+r.portal).toLowerCase().includes(q))&&(!f||r.stage===f));x.sort((a,b)=>{let pa=pipelineRows.findIndex(p=>p.cycle_id===a.pipeline),pb=pipelineRows.findIndex(p=>p.cycle_id===b.pipeline);if(pa!==pb){if(pa<0)return 1;if(pb<0)return -1;return pa-pb}return new Date(b.created||0)-new Date(a.created||0)});counts(x);jobCount.textContent="Showing "+x.length+" jobs";jobsTitle.textContent=selectedDate?"Jobs — "+labelDate(selectedDate):"Jobs — All Dates";jobs.innerHTML=x.map(r=>'<div class="row"><div class="jobInfo"><div class="title">'+esc(r.title)+'</div><div class="meta"><span class="company">'+esc(r.company)+'</span><span class="metaDot">•</span><span>'+esc(r.portal)+'</span><span class="metaDot">•</span><span>'+esc(pipelineName(r.pipeline))+'</span><span class="metaDot">•</span><span>'+esc(postedLabel(r))+'</span></span></div></div><div class="statusCell"><span class="badge '+cls(r.stage)+'">'+esc(r.stage)+'</span></div><div class="created">'+(r.applied_at?niceDate(r.applied_at):"—")+'</div><div class="actions">'+(r.url?'<a class="btn primary" href="'+esc(r.url)+'" target="_blank">Open Job</a>':'<span></span>')+(r.resume_url?'<a class="btn" href="'+r.resume_url+'" target="_blank">View Resume</a>':'<span></span>')+(r.stage!=="Applied"?'<button class="btn" data-job-key="'+esc(r.key)+'">✓ Mark Applied</button>':'<span class="badge applied">✓ Applied</span>')+'<div class="menuWrap"><button class="btn" data-menu="1">•••</button><div class="menu"><button class="btn deleteBtn" data-delete-key="'+esc(r.key)+'">Delete</button></div></div></div></div>').join("")||'<div class="empty">No jobs in this view.</div>';let p=pipelineRows.filter(r=>!selectedDate||dateKey(r.created)===selectedDate);pipeCount.textContent=p.length+" runs";pipeTitle.textContent=selectedDate?"Pipeline Runs — "+labelDate(selectedDate):"Pipeline Runs — All Dates";pipes.innerHTML=p.map((r,i)=>'<div class="pipe"><b><span class="green">●</span> '+(i===0?"Latest Run":"Pipeline Run")+'</b><div>'+niceDate(r.created)+'</div><div>'+esc(r.discovered)+' jobs found · '+esc(r.eligible)+' eligible</div><div>'+esc(r.ready)+' ready to apply</div><div style="margin-top:12px"><button class="btn" data-pipeline="'+esc(r.cycle_id)+'">View Jobs →</button></div></div>').join("")||'<div class="empty">No pipeline records for this date.</div>'}
async function markApplied(key,btn){if(!confirm("Mark this application as Applied?"))return;btn.disabled=true;try{let r=await fetch("/api/applications/"+encodeURIComponent(key)+"/confirm-submitted",{method:"POST"});if(!r.ok)throw 0;await load()}catch(e){alert("Could not update status.");btn.disabled=false}}
async function deleteRow(key){if(!confirm("Delete only this application from the dashboard?"))return;let r=await fetch("/api/applications/"+encodeURIComponent(key),{method:"DELETE"});if(r.ok)load();else alert("Could not delete this application.")}
async function loadApplications(){let url="/api/applications";if(selectedPipeline)url+="?pipeline="+encodeURIComponent(selectedPipeline);else if(selectedDate)url+="?date="+encodeURIComponent(selectedDate);let a=await fetch(url,{cache:"no-store"}).then(r=>r.json());rows=a.applications||[];renderTabs();render()}
async function load(){if(selectedDate===null)selectedDate=dateKey(new Date());let [p,a]=await Promise.all([fetch("/api/pipelines",{cache:"no-store"}).then(r=>r.json()),(async()=>{let url="/api/applications";if(selectedPipeline)url+="?pipeline="+encodeURIComponent(selectedPipeline);else if(selectedDate)url+="?date="+encodeURIComponent(selectedDate);return fetch(url,{cache:"no-store"}).then(r=>r.json())})()]);pipelineRows=p.pipelines||[];rows=a.applications||[];renderTabs();render()}
pipes.addEventListener("click",async e=>{let b=e.target.closest("[data-pipeline]");if(!b)return;selectedPipeline=b.dataset.pipeline;let pr=pipelineRows.find(r=>r.cycle_id===selectedPipeline);if(pr)selectedDate=dateKey(pr.created);await loadApplications();document.getElementById("jobsSection").scrollIntoView({behavior:"smooth"})});tabs.addEventListener("click",async e=>{let allBtn=e.target.closest("[data-all-dates]");if(allBtn){selectedPipeline="";selectedDate="";await loadApplications();return}let prev=e.target.closest("[data-prev]"),next=e.target.closest("[data-next]");if(prev||next){selectedPipeline="";let base=selectedDate||dateKey(new Date()),d=new Date(base+"T12:00:00");d.setDate(d.getDate()+(next?1:-1));selectedDate=d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");await loadApplications();return}let b=e.target.closest("[data-date]");if(!b)return;selectedPipeline="";selectedDate=b.getAttribute("data-date");await loadApplications()});jobs.addEventListener("click",e=>{let m=e.target.closest("[data-menu]");if(m){m.nextElementSibling.classList.toggle("open");return}let b=e.target.closest("[data-job-key]");if(b){markApplied(b.dataset.jobKey,b);return}let d=e.target.closest("[data-delete-key]");if(d)deleteRow(d.dataset.deleteKey)});datePicker.addEventListener("change",async()=>{selectedPipeline="";selectedDate=datePicker.value||"";await loadApplications()});search.oninput=render;filter.onchange=render;todayLabel.textContent=new Date().toLocaleDateString([],{weekday:"short",month:"short",day:"numeric",year:"numeric"});load();setInterval(load,10000);
</script></body></html>""")

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--host",default="0.0.0.0");ap.add_argument("--port",type=int,default=int(os.getenv("PORT","8765")));a=ap.parse_args()
    uvicorn.run("app.dashboard:app",host=a.host,port=a.port,reload=False)

if __name__=="__main__":main()
