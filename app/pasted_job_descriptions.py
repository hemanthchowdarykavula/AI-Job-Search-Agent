"""Dashboard path 3: paste a job description and run the shared production resume pipeline.

No discovery, ATS fetch, posted-date, or application-eligibility verification is
claimed.  Resume generation, audits, formatting, and DOCX/PDF gates are shared
with paths 1 and 2 via manual_job_links.run_shared_resume_pipeline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app import manual_job_links

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.getenv("JOB_AGENT_STATE_DIR", ROOT / "generated"))
JD_DIR = STATE_DIR / "pasted_job_descriptions"
STATE_FILE = JD_DIR / "jobs.json"
WORK_DIR = JD_DIR / "work"
ARTIFACT_DIR = JD_DIR / "resumes"
basic_auth = HTTPBasic()


def _authenticate(credentials: HTTPBasicCredentials = Depends(basic_auth)) -> None:
    """Protect paid LLM generation and private resume downloads by default."""
    token = os.getenv("PASTED_JD_ACCESS_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Configure PASTED_JD_ACCESS_TOKEN on the Railway dashboard service.")
    valid = secrets.compare_digest(credentials.username, "admin") and secrets.compare_digest(credentials.password, token)
    if not valid:
        raise HTTPException(401, "Invalid dashboard access token.", headers={"WWW-Authenticate": "Basic"})


router = APIRouter(dependencies=[Depends(_authenticate)])
_LOCK = threading.RLock()
_PROCESS_LOCK = threading.Lock()
PROCESSING = {"QUEUED", "GENERATING_RESUME"}


class DescriptionInput(BaseModel):
    description: str = Field(min_length=60, max_length=50000)
    title: str = Field(default="", max_length=180)
    company: str = Field(default="", max_length=180)
    url: str = Field(default="", max_length=2000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state() -> dict:
    with _LOCK:
        if STATE_FILE.is_file():
            try:
                value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(value, dict) and isinstance(value.get("jobs"), dict):
                    return value
            except (ValueError, OSError):
                pass
        return {"version": 1, "jobs": {}}


def _save(value: dict) -> None:
    with _LOCK:
        JD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)


def _title(value: str, description: str) -> str:
    title = " ".join(value.strip().split())
    if not title:
        for line in description.splitlines()[:15]:
            found = re.match(r"^\s*(?:role|job title|position|title)\s*[:=-]\s*(.{3,180})\s*$", line, re.I)
            if found:
                title = found.group(1).strip("* -")
                break
    return title[:180] if title else "Data Engineer"


def _public(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k != "description"}
    out["resume_available"] = bool(_artifact(row, "pdf"))
    out["docx_available"] = bool(_artifact(row, "docx"))
    out["resume_url"] = "/pasted-jd-resume/" + row["key"] + "/pdf" if out["resume_available"] else None
    out["docx_url"] = "/pasted-jd-resume/" + row["key"] + "/docx" if out["docx_available"] else None
    return out


def _artifact(row: dict, kind: str) -> Path | None:
    field = "pdf_path" if kind == "pdf" else "resume_path"
    value = str(row.get(field) or "")
    if not value:
        return None
    path = Path(value).resolve()
    root = ARTIFACT_DIR.resolve()
    if path.suffix.lower() != "." + kind or not path.is_file():
        return None
    if root not in path.parents:
        return None
    return path


def _get(key: str) -> tuple[dict, dict]:
    state = _state()
    row = state["jobs"].get(key)
    if row is None:
        raise HTTPException(404, "Pasted description not found")
    return state, row


def add_description(data: DescriptionInput) -> dict:
    description = data.description.strip()
    if len(description) < 60:
        raise ValueError("Paste at least 60 characters of the job description.")
    title = _title(data.title, description)
    company = " ".join(data.company.split()) or "Employer Not Specified"
    url = manual_job_links._normalize_url(data.url) if data.url.strip() else ""
    # A digest avoids duplicate paid generations for the same JD, company, and title.
    digest = hashlib.sha256(json.dumps([company.lower(), title.lower(), description], ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
    key = "pasted-jd-" + digest
    with _LOCK:
        state = _state()
        if key in state["jobs"]:
            return _public(state["jobs"][key])
        row = {
            "key": key, "external_id": key, "company": company, "title": title,
            "description": description, "url": url, "source": "pasted_jd",
            "application_route": "PASTED_JD", "status": "QUEUED",
            "next_action": None, "application_status": "NOT_STARTED",
            "description_complete": True, "description_usable": True,
            "posting_verified": False, "manual_filters_bypassed": True,
            "created_at": _now(), "updated_at": _now(),
            "resume_path": None, "pdf_path": None, "error": None
        }
        state["jobs"][key] = row
        _save(state)
    return _public(row)


def process_description(key: str) -> dict:
    with _PROCESS_LOCK:
        state, row = _get(key)
        if row.get("status") in {"RESUME_READY", "READY_TO_APPLY"}:
            return _public(row)
        row["status"] = "GENERATING_RESUME"
        row["error"] = None
        row["updated_at"] = _now()
        state["jobs"][key] = row
        _save(state)
        try:
            raw = {
                "external_id": key, "company_key": row["company"],
                "company": row["company"], "title": row["title"],
                "description": row["description"],
                "source": "pasted_jd", "application_route": "PASTED_JD",
                "description_complete": True, "description_usable": True,
                "tailoring_mode": "FULL_JD", "url": row.get("url") or None,
                "original_url": row.get("url") or None,
                "manual_filters_bypassed": True,
                # Do not turn a user-pasted JD into an assertion that an official
                # vacancy, posting date, company, or eligibility was verified.
                "posting_verified": False
            }
            result = manual_job_links.run_shared_resume_pipeline(
                raw, artifact_dir=ARTIFACT_DIR, work_dir=WORK_DIR
            )
            ready = result.get("next_action") == "READY_TO_APPLY"
            row.update({
                "status": "RESUME_READY" if ready else result.get("next_action") or "HOLD_RESUME_ERROR",
                "next_action": result.get("next_action"),
                "resume_path": str(result.get("resume_path") or ""),
                "pdf_path": str(result.get("pdf_path") or ""),
                "ats_audit": result.get("ats_audit"),
                "artifact_validation": result.get("artifact_validation"),
                "resume_tailoring_policy": result.get("resume_tailoring_policy"),
                "error": None if ready else (result.get("ats_audit") or {}).get("error")
                         or (result.get("artifact_validation") or {}).get("reason")
                         or "Resume did not pass its production quality gates.",
                "updated_at": _now(),
            })
        except Exception as exc:
            row.update({"status": "HOLD_RESUME_ERROR", "error": str(exc), "updated_at": _now()})
        with _LOCK:
            state = _state()
            if key in state["jobs"]:  # Never resurrect a job deleted while processing.
                current = state["jobs"][key]
                if current.get("application_status") == "SUBMITTED_CONFIRMED":
                    row["application_status"] = "SUBMITTED_CONFIRMED"
                    row["submitted_at"] = current.get("submitted_at")
                state["jobs"][key] = row
                _save(state)
        return _public(row)


def list_descriptions() -> dict:
    rows = [_public(r) for r in _state()["jobs"].values()]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {
        "jobs": rows,
        "counts": {
            "total": len(rows),
            "ready": sum(r["status"] == "RESUME_READY" for r in rows),
            "processing": sum(r["status"] in PROCESSING for r in rows),
            "attention": sum(r["status"] not in PROCESSING | {"RESUME_READY"} for r in rows),
            "applied": sum(r.get("application_status") == "SUBMITTED_CONFIRMED" for r in rows)
        }
    }


@router.on_event("startup")
def recover_interrupted() -> None:
    # An app redeployment during generation should not leave a permanent spinner.
    state = _state()
    keys = [key for key, row in state["jobs"].items() if row.get("status") in PROCESSING]
    if keys:
        threading.Thread(target=lambda: [process_description(key) for key in keys],
                         daemon=True, name="pasted-jd-recovery").start()


@router.get("/api/pasted-jds")
def api_list():
    return list_descriptions()


@router.post("/api/pasted-jds")
def api_add(data: DescriptionInput, background_tasks: BackgroundTasks):
    try:
        row = add_description(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if row["status"] == "QUEUED":
        background_tasks.add_task(process_description, row["key"])
    return {"ok": True, "job": row}


@router.post("/api/pasted-jds/{key}/retry")
def api_retry(key: str, background_tasks: BackgroundTasks):
    with _LOCK:
        state, row = _get(key)
        if row.get("status") in PROCESSING:
            return {"ok": True, "job": _public(row), "queued": False}
        row.update({
            "status": "QUEUED", "next_action": None, "error": None,
            "resume_path": None, "pdf_path": None, "ats_audit": None,
            "artifact_validation": None, "updated_at": _now()
        })
        state["jobs"][key] = row
        _save(state)
    background_tasks.add_task(process_description, key)
    return {"ok": True, "job": _public(row), "queued": True}


@router.post("/api/pasted-jds/{key}/confirm-submitted")
def api_confirm(key: str):
    with _LOCK:
        state, row = _get(key)
        if row.get("status") != "RESUME_READY":
            raise HTTPException(409, "Resume is not ready.")
        if not row.get("url"):
            raise HTTPException(409, "Add a job application URL before marking as applied.")
        row["application_status"] = "SUBMITTED_CONFIRMED"
        row["submitted_at"] = _now()
        row["updated_at"] = _now()
        state["jobs"][key] = row
        _save(state)
    return {"ok": True, "job": _public(row)}


@router.delete("/api/pasted-jds/{key}")
def api_delete(key: str):
    with _LOCK:
        state, _ = _get(key)
        state["jobs"].pop(key)
        _save(state)
    safe = manual_job_links._safe_key(key)
    shutil.rmtree(WORK_DIR / safe, ignore_errors=True)
    shutil.rmtree(ARTIFACT_DIR / safe, ignore_errors=True)
    return {"ok": True, "deleted": key}


@router.get("/pasted-jd-resume/{key}/{kind}")
def download_resume(key: str, kind: str):
    if kind not in {"docx", "pdf"}:
        raise HTTPException(404, "Unknown file format")
    _, row = _get(key)
    path = _artifact(row, kind)
    if not path:
        raise HTTPException(404, "Validated resume artifact is not available")
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
             if kind == "docx" else "application/pdf")
    disposition = "attachment" if kind == "docx" else "inline"
    return FileResponse(path, media_type=media, headers={
        "Content-Disposition": disposition + '; filename="' + path.name + '"'
    })


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paste Job Description · Auto Apply</title>
<style>
:root{--bg:#07111f;--panel:#0b1726;--line:#21344a;--muted:#91a4bc;--text:#edf3fb;--blue:#2f73df}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font:14px Inter,Segoe UI,Arial,sans-serif;color:var(--text)}
.app{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100vh}
.side{background:#0a1625;border-right:1px solid var(--line);padding:22px 14px}
.brand{font-size:20px;font-weight:850;padding:0 8px 24px}.brand small{display:block;color:var(--muted);font-size:11px;font-weight:500;margin-top:4px}
.nav{display:grid;gap:6px}.nav a{color:#c7d2e2;text-decoration:none;padding:11px 12px;border-radius:8px;font-weight:650}.nav a.active,.nav a:hover{background:#173967;color:#fff}
main{padding:26px clamp(16px,3vw,40px);min-width:0;max-width:1450px;width:100%;margin:auto}
h1{font-size:27px;margin:0 0 10px}.muted{color:var(--muted);line-height:1.5}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:20px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.field{display:flex;flex-direction:column;gap:6px}
label{font-size:12px;font-weight:750;color:#c3d3e8}
input,textarea{font:inherit;background:#071321;color:var(--text);border:1px solid #2a4058;border-radius:8px;padding:11px 12px;width:100%;outline:none}
textarea{min-height:230px;resize:vertical;line-height:1.5}input:focus,textarea:focus{border-color:#4b8bf0}
button,.btn{font:inherit;border:1px solid #30465f;background:#13253a;color:#dce7f5;padding:9px 12px;border-radius:7px;cursor:pointer;text-decoration:none;display:inline-block}
button.primary{background:#2f73df;border-color:#4388f4;color:white;font-weight:750}button:disabled{opacity:.6;cursor:default}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}.stat{background:#0d1a2b;border:1px solid #263950;padding:12px 18px;border-radius:9px;min-width:120px}.stat b{font-size:21px;display:block;margin-top:5px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.toolbar input{max-width:200px}.toolbar select{background:#0d1a2b;color:white;border:1px solid #2a4058;border-radius:7px;padding:10px}
.item{border-top:1px solid #17283a;padding:16px 0;display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
.item:first-of-type{border-top:0}.item strong{font-size:15px;display:block;margin-bottom:6px}
.meta{font-size:12px;color:#a9b8ca;line-height:1.5}.status{font-size:11px;font-weight:800;margin-top:7px;color:#66deb0}.warn{color:#f2ba7b}
.itemActions{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}
small{color:#91a4bc}#message{margin-left:10px}
@media(max-width:820px){.app{display:block}.side{padding:12px;height:auto}.nav{display:flex;overflow-x:auto}.nav a{white-space:nowrap}.grid{grid-template-columns:1fr}.item{display:block}.itemActions{margin-top:12px;justify-content:flex-start}main{padding:18px}}
</style></head><body>
<div class="app"><aside class="side">
<div class="brand">💼 Auto Apply<small>Job Application Manager</small></div>
<nav class="nav"><a href="/">⌂ Job Discovery</a><a href="/manual-links">🔗 Manual Job Links</a>
<a class="active" href="/pasted-jds">📝 Paste Job Description</a></nav></aside>
<main><h1>Paste Job Description</h1>
<p class="muted">Paste any complete job description. This path bypasses job discovery and web extraction,
but uses the same master-resume tailoring, audit, formatting, and DOCX/PDF validation pipeline as the other two paths.
An unverified pasted description is a resume draft, not a verified open job.
Sign in with username <strong>admin</strong> and the Railway <strong>PASTED_JD_ACCESS_TOKEN</strong>
when your browser requests access.</p>
<section class="panel"><form id="form">
<div class="grid"><div class="field"><label for="title">Job title (optional)</label>
<input id="title" maxlength="180" placeholder="e.g. Data Engineer on GCP with GenAI"></div>
<div class="field"><label for="company">Employer (optional)</label>
<input id="company" maxlength="180" placeholder="Employer, if known"></div></div>
<div class="field" style="margin-top:12px"><label for="url">Application URL (optional)</label>
<input id="url" maxlength="2000" placeholder="https://company.com/careers/job/..."></div>
<div class="field" style="margin-top:12px"><label for="description">Job description</label>
<textarea id="description" minlength="60" maxlength="50000" required
placeholder="Paste the role description, requirements, technologies, and responsibilities here..."></textarea></div>
<div class="actions"><button class="primary" id="submit" type="submit">Generate Resume</button>
<button id="refresh" type="button">Refresh</button><small id="message">Generation runs on Railway and may take several minutes.</small></div>
</form></section>
<div class="stats" id="stats"></div>
<section class="panel"><div class="toolbar"><strong>Saved descriptions & resumes</strong>
<select id="statusFilter" aria-label="Filter status"><option value="">All statuses</option>
<option value="RESUME_READY">Resume ready</option><option value="processing">Processing</option>
<option value="attention">Needs attention</option></select>
<input id="dateFilter" type="date" aria-label="Filter creation date">
<button type="button" id="clearDate">All dates</button></div><div id="jobs"></div></section>
</main></div>
<script>
const $=id=>document.getElementById(id);
const escapeHtml=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=s=>s?new Date(s).toLocaleString():'—';
const nyDate=s=>{if(!s)return '';const d=new Date(s);return new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(d)};
let rows=[];
function render(){
 const f=$('statusFilter').value, date=$('dateFilter').value, working=['QUEUED','GENERATING_RESUME'];
 const chosen=rows.filter(r=>(!date||nyDate(r.created_at)===date)&&(!f||
   (f==='processing'?working.includes(r.status):
   f==='attention'?(!working.includes(r.status)&&r.status!=='RESUME_READY'):r.status===f)));
 const num=(q)=>rows.filter(q).length;
 $('stats').innerHTML=[
  ['Descriptions',rows.length],['Resume ready',num(r=>r.status==='RESUME_READY')],
  ['Processing',num(r=>working.includes(r.status))],
  ['Needs attention',num(r=>!working.includes(r.status)&&r.status!=='RESUME_READY')]
 ].map(x=>'<div class="stat"><small>'+x[0]+'</small><b>'+x[1]+'</b></div>').join('');
 $('jobs').innerHTML=chosen.map(r=>{
  const ready=r.status==='RESUME_READY', workingNow=working.includes(r.status), applied=r.application_status==='SUBMITTED_CONFIRMED';
  const label=applied?'Applied':ready?'Resume ready':workingNow?'Generating…':'Needs attention';
  const links=ready?((r.resume_url?'<a class="btn" target="_blank" rel="noopener" href="'+escapeHtml(r.resume_url)+'">View PDF</a>':'')
      +(r.docx_url?'<a class="btn" href="'+escapeHtml(r.docx_url)+'">Download Word</a>':'')):'';
  const open=r.url?'<a class="btn" href="'+escapeHtml(r.url)+'" target="_blank" rel="noopener">Open Job</a>':'';
  const apply=ready&&r.url&&!applied?'<button data-act="applied" data-key="'+escapeHtml(r.key)+'">Mark Applied</button>':'';
  const retry=!workingNow&&!ready?'<button data-act="retry" data-key="'+escapeHtml(r.key)+'">Retry</button>':'';
  return '<div class="item"><div><strong>'+escapeHtml(r.title)+'</strong><div class="meta">'+
     escapeHtml(r.company)+' · Created '+escapeHtml(when(r.created_at))+
     (applied?' · Applied '+escapeHtml(when(r.submitted_at)):'')+
     '</div><div class="status '+(ready?'':'warn')+'">'+escapeHtml(label)+'</div>'+
     (r.error?'<div class="meta warn">'+escapeHtml(r.error)+'</div>':'')+
     '</div><div class="itemActions">'+open+links+apply+retry+
     '<button data-act="delete" data-key="'+escapeHtml(r.key)+'">Delete</button></div></div>';
 }).join('')||'<p class="muted">No saved descriptions match these filters.</p>';
}
async function refresh(){try{const r=await fetch('/api/pasted-jds',{cache:'no-store'});if(!r.ok)throw Error('Unable to load descriptions');rows=(await r.json()).jobs||[];render()}catch(e){$('message').textContent=e.message}}
$('form').addEventListener('submit',async e=>{
 e.preventDefault();const b=$('submit');b.disabled=true;$('message').textContent='Submitting description…';
 try{
  const r=await fetch('/api/pasted-jds',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({title:$('title').value,company:$('company').value,url:$('url').value,description:$('description').value})});
  const data=await r.json();if(!r.ok)throw Error(data.detail||'Could not start resume generation');
  $('description').value='';$('message').textContent='Submitted. Check its status below.';
  await refresh();
 }catch(err){$('message').textContent=err.message}finally{b.disabled=false}
});
$('jobs').addEventListener('click',async e=>{
 const btn=e.target.closest('button[data-act]');if(!btn)return;
 const key=encodeURIComponent(btn.dataset.key), action=btn.dataset.act;
 if(action==='delete'&&!confirm('Delete this description and its resume files?'))return;
 if(action==='applied'&&!confirm('Confirm you submitted this application?'))return;
 btn.disabled=true;
 try{
  const path='/api/pasted-jds/'+key+(action==='retry'?'/retry':action==='applied'?'/confirm-submitted':'');
  const r=await fetch(path,{method:action==='delete'?'DELETE':'POST'});
  const data=await r.json();if(!r.ok)throw Error(data.detail||'Action failed');await refresh();
 }catch(err){alert(err.message);btn.disabled=false}
});
$('statusFilter').addEventListener('change',render);
$('dateFilter').addEventListener('change',render);
$('clearDate').addEventListener('click',()=>{$('dateFilter').value='';render()});
$('refresh').addEventListener('click',refresh);
refresh();setInterval(refresh,10000);
</script></body></html>'''


@router.get("/pasted-jds", response_class=HTMLResponse)
def page():
    return HTMLResponse(PAGE)
