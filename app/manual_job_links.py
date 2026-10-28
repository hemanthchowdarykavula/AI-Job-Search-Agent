from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import threading
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows/local fallback
    fcntl = None
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app import batch_prepare
from app.ats_resolver import resolve_original_ats
from app.jd_finalizer import (
    _best_resolved_description,
    _clean_html,
    _fetch_public_page,
    _jsonld_jobpostings,
    _looks_like_complete_jd,
    _looks_like_usable_jd,
    enrich_authoritative_job_metadata,
    resolve_full_jd,
)
from app.source_registry import detect_ats

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.getenv("JOB_AGENT_STATE_DIR", ROOT / "generated"))
MANUAL_DIR = STATE_DIR / "manual_job_links"
STATE_FILE = MANUAL_DIR / "manual_jobs.json"
WORK_DIR = MANUAL_DIR / "work"
ARTIFACT_DIR = MANUAL_DIR / "resumes"
router = APIRouter()
_STATE_LOCK = threading.RLock()
_PREPARE_LOCK = threading.Lock()
_BATCH_LOCK = threading.Lock()

_PROCESSING_STATUSES = {"PENDING", "JD_READY", "FETCHING_JD", "GENERATING_RESUME"}
_PROVIDER_LABELS = {
    "workday": "Workday", "greenhouse": "Greenhouse", "lever": "Lever",
    "ashby": "Ashby", "smartrecruiters": "SmartRecruiters", "icims": "iCIMS",
    "jobvite": "Jobvite", "dayforce": "Dayforce", "oracle": "Oracle",
    "successfactors": "SuccessFactors", "ultipro": "UKG / UltiPro",
    "ultipro_ukg": "UKG / UltiPro", "ukg": "UKG", "radancy": "Radancy",
    "career_site": "Employer career site",
}
_ATS_VENDOR_NAMES = {
    "workday", "myworkdayjobs", "greenhouse", "lever", "ashby", "smartrecruiters",
    "icims", "jobvite", "dayforce", "dayforcehcm", "oracle", "oraclecloud",
    "successfactors", "ultipro", "ukg", "radancy",
}
_AGGREGATOR_NAMES = {
    "adzuna", "dice", "indeed", "linkedin", "ziprecruiter", "monster",
    "wellfound", "built in", "builtin", "yc jobs", "y combinator",
    "glassdoor", "simplyhired", "careerbuilder",
}
_AGGREGATOR_HOSTS = (
    "adzuna.com", "dice.com", "indeed.com", "linkedin.com", "ziprecruiter.com",
    "monster.com", "wellfound.com", "builtin.com", "ycombinator.com",
    "glassdoor.com", "simplyhired.com", "careerbuilder.com",
)
_GENERIC_JOB_TITLES = {
    "job", "job details", "job detail", "job search", "jobs", "careers",
    "career", "recruitment", "career opportunities", "job opening",
    "finance, service, engineering, & developer jobs",
}


def _collapse_label(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip(" |–—-\t\r\n")


def _slug_label(value: str | None) -> str:
    raw = _collapse_label(value)
    if not raw:
        return ""
    raw = raw.split("|", 1)[0]
    raw = raw.split(".", 1)[0]
    raw = re.sub(r"[_-]+", " ", raw).strip()
    if not raw or len(raw) > 64 or re.fullmatch(r"[0-9a-fA-F-]{16,}", raw):
        return ""
    return raw if any(ch.isupper() for ch in raw[1:]) else raw.title()


def _clean_company_label(value: str | None) -> str:
    label = _collapse_label(value)
    if not label:
        return ""
    label = re.sub(
        r"(?i)\s+(?:global\s+career\s+site|career\s+site|careers|jobs)\s*$",
        "",
        label,
    ).strip()
    low = label.lower().strip()
    if not label or low in {"unknown company", "company", "job search", "job details"}:
        return ""
    if low in _ATS_VENDOR_NAMES or low in _AGGREGATOR_NAMES:
        return ""
    if any(low == name or low.startswith(name + " ") for name in _AGGREGATOR_NAMES):
        return ""
    return label[:180]


def _company_compare_key(value: str | None) -> str:
    clean = _clean_company_label(value)
    if not clean:
        return ""
    low = clean.lower()
    low = re.sub(r"\b(?:incorporated|inc|corp(?:oration)?|llc|ltd|limited|plc|company|co)\b\.?$", "", low).strip()
    return re.sub(r"[^a-z0-9]+", "", low)


def _verified_company(
    *,
    resolved_meta: dict,
    initial_meta: dict,
    provider: str | None,
    identifier: str | None,
    effective_url: str,
    submitted_url: str,
    raw_company: str | None,
    raw_company_source: str | None = None,
    resolved_site_name: str | None = None,
    resolved_fallback: str | None = None,
    initial_site_name: str | None = None,
    initial_fallback: str | None = None,
) -> tuple[str, str]:
    """Return a company only when its source is strong or independently corroborated."""
    for value, source in (
        (resolved_meta.get("company"), "resolved_jobposting"),
        (initial_meta.get("company"), "submitted_jobposting"),
    ):
        clean = _clean_company_label(value)
        if clean:
            return clean, source

    raw_clean = _clean_company_label(raw_company)
    raw_source = str(raw_company_source or "").lower()
    trusted_provider_company = bool(raw_source) and any(
        marker in raw_source
        for marker in (
            "exact", "_api", "public_detail", "public_posting",
            "public_account", "embedded_opportunity", "details_jsonld",
            "verified_employer_source_match",
        )
    )
    if raw_clean and trusted_provider_company:
        raw_key = _company_compare_key(raw_clean)
        provider_key = _company_compare_key((provider or "").replace("_", " "))
        identifier_key = _company_compare_key(_slug_label(identifier))
        generic_keys = {
            _company_compare_key("Company"),
            _company_compare_key("Unknown Company"),
            _company_compare_key("Workable"),
            _company_compare_key("Paylocity"),
            _company_compare_key("BambooHR"),
            _company_compare_key("UltiPro"),
            _company_compare_key("UKG"),
        }
        if raw_key and raw_key not in generic_keys and raw_key not in {provider_key, identifier_key}:
            return raw_clean, "provider_exact_metadata"

    ats_hint = _ats_company_hint(provider, identifier, effective_url) if provider and identifier else ""
    if ats_hint:
        return ats_hint, "ats_tenant"

    # A direct domain is only a hint; never accept it by itself. Require
    # corroboration from independent page metadata to avoid treating a vendor,
    # staffing portal, or white-label career site as the employer.
    direct_hint = _ats_company_hint(None, None, effective_url)

    # Portal/page-title/domain values are accepted only when two independent
    # signals agree after legal-suffix/punctuation normalization.
    low_confidence = [
        direct_hint,
        resolved_site_name,
        resolved_fallback,
        initial_site_name,
        initial_fallback,
    ]
    grouped: dict[str, list[str]] = {}
    for value in low_confidence:
        clean = _clean_company_label(value)
        key = _company_compare_key(clean)
        if key:
            grouped.setdefault(key, []).append(clean)
    matches = [values for values in grouped.values() if len(values) >= 2]
    if matches:
        return matches[0][0], "corroborated_page_metadata"

    return "Company", "unverified"


def _clean_job_title(value: str | None) -> str:
    label = _collapse_label(value)
    if not label:
        return ""
    low = label.lower()
    if low in _GENERIC_JOB_TITLES or low in {"career search", "job search", "search jobs", "search careers", "career site", "job listings"}:
        return ""
    # Employer/portal domains are not role titles (e.g. Datacolor.ai).
    if re.fullmatch(r"(?i)(?:[a-z0-9-]+\.)+(?:com|net|org|io|ai|co|jobs|careers)",label):
        return ""
    if re.fullmatch(r"(?i)(?:jobs?|careers?|recruitment)(?:\s+at\s+.+)?", label):
        return ""
    return label[:180]


def _has_verified_job_title(value: str | None) -> bool:
    clean = _clean_job_title(value)
    return bool(clean and clean.casefold() not in {"job opening", "job from supplied link"})


def _title_from_description(text: str, company: str | None = None) -> str:
    """Recover a job title from explicit employer wording when page metadata is client-rendered."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    patterns = (
        r"(?i)\bcurrently\s+seeking\s+(?:an?\s+)?([^.;\n]{3,140})",
        r"(?i)\bis\s+seeking\s+(?:an?\s+)?([^.;\n]{3,140})",
        r"(?i)\bseeking\s+(?:an?\s+)?([^.;\n]{3,140})",
        r"(?i)\bas\s+(?:an?\s+)?([^,.;\n]{3,120}),\s+you\b",
    )
    role_hint = re.compile(r"(?i)\b(ai|ml|data|analytics|engineer|engineering|developer|architect|scientist|platform|software)\b")
    for pattern in patterns:
        match = re.search(pattern, value[:12000])
        if not match:
            continue
        candidate = _clean_job_title(match.group(1))
        if candidate and role_hint.search(candidate):
            return candidate
    return ""


def _heading_job_title(page: str) -> str:
    role_hint = re.compile(
        r"(?i)\b(data|analytics|engineer|engineering|developer|architect|scientist|platform|etl|database|software)\b"
    )
    for match in re.finditer(r"(?is)<h[12][^>]*>(.*?)</h[12]>", page or ""):
        candidate = _clean_job_title(_clean_html(match.group(1)))
        if candidate and role_hint.search(candidate):
            return candidate
    return ""


def _ats_company_hint(provider: str | None, identifier: str | None, url: str) -> str:
    if provider in {"workday", "greenhouse", "lever", "ashby", "smartrecruiters", "icims", "jobvite"}:
        label = _slug_label(identifier)
        if _clean_company_label(label):
            return _clean_company_label(label)
    host = (urlsplit(url or "").hostname or "").lower()
    if host and not any(vendor in host for vendor in _ATS_VENDOR_NAMES) and not any(
        host == agg or host.endswith("." + agg) for agg in _AGGREGATOR_HOSTS
    ):
        parts = [
            p for p in host.split(".")
            if p not in {"www", "jobs", "job", "careers", "career", "apply", "recruiting", "recruitment"}
        ]
        if parts:
            return _clean_company_label(_slug_label(parts[0]))
    return ""


def _ats_display(provider: str | None, identifier: str | None) -> tuple[str, str]:
    label = _PROVIDER_LABELS.get(provider or "", (provider or "Direct employer page").replace("_", " ").title())
    tenant = _slug_label(identifier)
    if tenant and tenant.lower() in _ATS_VENDOR_NAMES:
        tenant = ""
    return label, tenant


def _is_processing(row: dict) -> bool:
    return row.get("status") in _PROCESSING_STATUSES and row.get("application_status") != "SUBMITTED_CONFIRMED"


class LinksInput(BaseModel):
    links: list[str] | str


class ManualEdit(BaseModel):
    url: str | None = None
    company: str | None = None
    title: str | None = None
    location: str | None = None
    employment_type: str | None = None
    description: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    with _STATE_LOCK:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
                return data
        except Exception:
            pass
        return {"version": 1, "jobs": {}}


def _save_state(data: dict) -> None:
    with _STATE_LOCK:
        MANUAL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)


def _normalize_url(value: str) -> str:
    raw = (value or "").strip()
    # A pasted Markdown-escaped link may have a single leading backslash.
    if raw.startswith("\\http://") or raw.startswith("\\https://"):
        raw = raw[1:]
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Job URL must use http:// or https://")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Local/private job URLs are not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast):
        raise ValueError("Local/private job URLs are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid job URL") from exc
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _split_links(value: list[str] | str) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[\s]+", value or "")
    out, seen = [], set()
    for item in items:
        if not str(item).strip():
            continue
        url = _normalize_url(str(item))
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _key_for_url(url: str) -> str:
    digest = hashlib.sha256(_normalize_url(url).encode()).hexdigest()[:20]
    return f"manual:{digest}"


def _safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


def _meta(page: str, name: str) -> str:
    q = re.escape(name)
    patterns = (
        rf'''(?is)<meta[^>]+(?:name|property)=["']{q}["'][^>]+content=["']([^"']+)["']''',
        rf'''(?is)<meta[^>]+content=["']([^"']+)["'][^>]+(?:name|property)=["']{q}["']''',
    )
    for pattern in patterns:
        m = re.search(pattern, page or "")
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _page_title(page: str) -> str:
    value = _meta(page, "og:title") or _meta(page, "twitter:title")
    if value:
        return value
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page or "")
    return _clean_html(m.group(1)) if m else ""


def _jsonld_metadata(page: str) -> dict:
    nodes = _jsonld_jobpostings(page)
    if not nodes:
        return {}
    node = max(nodes, key=lambda n: len(str(n.get("description") or "")))
    org = node.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else ""
    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")
    locations = []
    locs = node.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if isinstance(locs, dict) else []
    if str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        locations.append("Remote")
    for loc in locs:
        address = loc.get("address") if isinstance(loc, dict) else None
        if not isinstance(address, dict):
            continue
        text = ", ".join(
            str(address.get(k)).strip()
            for k in ("addressLocality", "addressRegion", "addressCountry")
            if address.get(k)
        )
        if text and text not in locations:
            locations.append(text)
    return {
        "title": str(node.get("title") or node.get("name") or "").strip(),
        "company": str(company or "").strip(),
        "description": _clean_html(str(node.get("description") or "")),
        "location": " / ".join(locations),
        "employment_type": node.get("employmentType"),
        "posted_at": node.get("datePosted"),
        "requisition_id": str(identifier or "").strip() or None,
        "jsonld_url": str(node.get("url") or "").strip() or None,
    }


def _fallback_title_company(page: str) -> tuple[str, str]:
    text = _page_title(page)
    bits = [x.strip() for x in re.split(r"\s+[|–—-]\s+", text) if x.strip()]
    if not bits:
        return "", ""
    title = _clean_job_title(bits[0])
    company = next(
        (clean for value in reversed(bits[1:]) if (clean := _clean_company_label(value))),
        "",
    )
    # Aggregators often use titles such as "Data Engineer at Acme - Adzuna".
    if not company:
        match = re.search(r"(?i)\b(?:at|with)\s+(.+?)(?=\s+[|–—-]\s+|$)", text)
        if match:
            company = _clean_company_label(match.group(1))
            if company and title:
                title = re.sub(r"(?i)\s+\b(?:at|with)\s+.+$", "", title).strip()
    return (title or bits[0][:180]), company


def fetch_manual_job(url: str) -> dict:
    """Resolve exactly the supplied link into clean employer metadata and the best public JD."""
    submitted = _normalize_url(url)
    try:
        page = _fetch_public_page(submitted) or ""
    except Exception:
        page = ""

    initial_meta = _jsonld_metadata(page) if page else {}
    title_fallback, company_fallback = _fallback_title_company(page) if page else ("", "")
    description = (
        initial_meta.get("description")
        or (_best_resolved_description(page, "manual_link") if page else "")
        or ""
    )
    raw = {
        "external_id": _key_for_url(submitted),
        "source": "manual_link",
        "company_key": initial_meta.get("company") or (_meta(page, "og:site_name") if page else "") or company_fallback or "Unknown company",
        "company": initial_meta.get("company") or (_meta(page, "og:site_name") if page else "") or company_fallback or "Unknown company",
        "title": initial_meta.get("title") or _heading_job_title(page) or title_fallback or "Job from supplied link",
        "description": description,
        "location": initial_meta.get("location") or "",
        "employment_type": initial_meta.get("employment_type"),
        "posted_at": initial_meta.get("posted_at"),
        "requisition_id": initial_meta.get("requisition_id"),
        "url": submitted,
        "original_url": initial_meta.get("jsonld_url") or submitted,
        "submitted_url": submitted,
        "manual_link": True,
        "manual_filters_bypassed": True,
        "application_route": "MANUAL_LINK",
    }

    # This is resolution, not discovery: follow only the supplied job and the
    # employer/ATS destination it exposes, then ask the production JD finalizer
    # for the strongest public description for that same job.
    try:
        resolved = resolve_original_ats(raw)
        if isinstance(resolved, dict):
            raw = resolved
        resolved = resolve_full_jd(raw)
        if isinstance(resolved, dict):
            raw = resolved
    except Exception:
        pass

    effective = raw.get("original_url") or raw.get("url") or submitted
    provider, identifier = detect_ats(effective)
    provider = provider or raw.get("ats_provider")
    identifier = identifier or raw.get("ats_identifier")

    # Re-read the final employer/ATS detail page when resolution changed the URL.
    # Structured JobPosting data there is more reliable than portal titles such
    # as "Job Details", "Dayforce Jobs", or "Global Career Site".
    resolved_page = page
    if effective and effective != submitted:
        try:
            resolved_page = _fetch_public_page(effective) or page
        except Exception:
            resolved_page = page
    resolved_meta = _jsonld_metadata(resolved_page) if resolved_page else {}
    authoritative_raw = raw
    resolved_title_fallback, resolved_company_fallback = (
        _fallback_title_company(resolved_page) if resolved_page else ("", "")
    )

    company, company_source = _verified_company(
        resolved_meta=resolved_meta,
        initial_meta=initial_meta,
        provider=provider,
        identifier=identifier,
        effective_url=effective,
        submitted_url=submitted,
        raw_company=raw.get("company_key") or raw.get("company"),
        raw_company_source=authoritative_raw.get("metadata_resolution_source"),
        resolved_site_name=_meta(resolved_page, "og:site_name") if resolved_page else "",
        resolved_fallback=resolved_company_fallback,
        initial_site_name=_meta(page, "og:site_name") if page else "",
        initial_fallback=company_fallback,
    )

    authoritative_description = (
        str(authoritative_raw.get("description") or "").strip()
        or str(resolved_meta.get("description") or "").strip()
        or str(initial_meta.get("description") or "").strip()
    )
    descriptions = [
        str(raw.get("description") or "").strip(),
        str(resolved_meta.get("description") or "").strip(),
        str(initial_meta.get("description") or "").strip(),
        str(description or "").strip(),
    ]
    if authoritative_description and _looks_like_usable_jd(authoritative_description, provider or "manual_link"):
        best_description = authoritative_description
        description_source = authoritative_raw.get("metadata_resolution_source") or ("jsonld_resolved" if resolved_meta.get("description") else "jsonld_submitted")
    else:
        best_description = max(descriptions, key=len, default="")
        description_source = "best_available"

    title_candidates = [
        (authoritative_raw.get("metadata_resolution_source") or "shared_finalizer", authoritative_raw.get("title")),
        ("resolved_jsonld", resolved_meta.get("title")),
        ("submitted_jsonld", initial_meta.get("title")),
        ("resolved_heading", _heading_job_title(resolved_page)),
        ("resolved_page_title", resolved_title_fallback),
        ("submitted_heading", _heading_job_title(page)),
        ("submitted_page_title", title_fallback),
        ("jd_text", _title_from_description(best_description, company)),
        ("resolver", raw.get("title")),
    ]
    title_source, title = next(
        ((source, clean) for source, value in title_candidates if (clean := _clean_job_title(value))),
        ("", ""),
    )
    title_verified = bool(title)
    if not title:
        title = "Job opening"
        title_source = "unverified"
    ats_label, ats_tenant = _ats_display(provider, identifier)

    raw["external_id"] = _key_for_url(submitted)
    raw["submitted_url"] = submitted
    raw["manual_link"] = True
    raw["manual_filters_bypassed"] = True
    raw["application_route"] = "MANUAL_LINK"
    raw["source"] = provider or raw.get("source") or "manual_link"
    raw["ats_provider"] = provider
    raw["ats_identifier"] = identifier
    raw["ats_label"] = ats_label
    raw["ats_tenant"] = ats_tenant
    raw["url"] = effective
    raw["original_url"] = effective
    raw["company_key"] = company
    raw["company"] = company
    raw["company_verified"] = company_source != "unverified"
    raw["company_source"] = company_source
    raw["title"] = title
    raw["title_verified"] = title_verified
    raw["title_source"] = title_source
    raw["description"] = best_description
    raw["description_source"] = description_source
    if authoritative_raw.get("location"):
        raw["location"] = authoritative_raw["location"]
    elif resolved_meta.get("location"):
        raw["location"] = resolved_meta["location"]
    if authoritative_raw.get("employment_type"):
        raw["employment_type"] = authoritative_raw["employment_type"]
    elif resolved_meta.get("employment_type"):
        raw["employment_type"] = resolved_meta["employment_type"]
    if authoritative_raw.get("requisition_id"):
        raw["requisition_id"] = authoritative_raw["requisition_id"]
    elif resolved_meta.get("requisition_id"):
        raw["requisition_id"] = resolved_meta["requisition_id"]
    if authoritative_raw.get("posted_at"):
        raw["posted_at"] = authoritative_raw["posted_at"]
    raw["metadata_resolution_source"] = authoritative_raw.get("metadata_resolution_source") or raw.get("metadata_resolution_source")

    complete = _looks_like_complete_jd(raw["description"], raw["source"])
    usable = complete or _looks_like_usable_jd(raw["description"], raw["source"])
    raw["description_complete"] = bool(complete)
    raw["description_usable"] = bool(usable)
    raw["tailoring_mode"] = "FULL_JD" if complete else "BASE_RESUME_CONSERVATIVE"
    raw["jd_fallback_reason"] = None if usable else (
        "No usable JD could be extracted from the supplied link or its resolved employer/ATS page; "
        "ignore this manual link and do not generate a resume."
    )
    return raw

def _bypass_eligibility() -> dict:
    item = {
        "eligible": True,
        "status": "BYPASSED_MANUAL_LINK",
        "reason": "Manual-link path intentionally bypasses discovery and eligibility filters.",
    }
    return {"eligible": True, "experience": dict(item), "sponsorship": dict(item), "manual_bypass": True}


def run_shared_resume_pipeline(
    raw: dict, *, artifact_dir: Path | None = None, work_dir: Path | None = None
) -> dict:
    """Reuse production resume generation, audits and export for manual paths 2 and 3.

    Optional directories isolate a pasted-JD artifact store from the job-link
    store without changing the shared formatter, ATS gates or PDF validation.
    """
    key = raw["external_id"]
    attempt = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe = _safe_key(key)
    artifact_dir = Path(artifact_dir) if artifact_dir is not None else ARTIFACT_DIR
    work_dir = Path(work_dir) if work_dir is not None else WORK_DIR
    work = work_dir / safe / attempt
    report = work / "finalized.json"
    manifest = work / "manifest.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({
        "manual_path": True,
        "discovery_bypassed": True,
        "eligibility_filters_bypassed": True,
        "results": [{"action": "FINAL_JD_VERIFIED", "job": raw, "eligibility": _bypass_eligibility()}],
    }, indent=2), encoding="utf-8")
    old_draft, old_final = batch_prepare.DRAFT_RESUME_DIR, batch_prepare.FINAL_RESUME_DIR
    with _PREPARE_LOCK:
        try:
            batch_prepare.DRAFT_RESUME_DIR = str(work / "drafts")
            batch_prepare.FINAL_RESUME_DIR = str(artifact_dir / safe / attempt)
            # Resume content selection, LLM prompting, retries, ATS audit, formatting,
            # and artifact validation are owned by the exact production path.
            rows = batch_prepare.prepare(str(report), str(manifest), external_id=key, limit=1)
        finally:
            batch_prepare.DRAFT_RESUME_DIR, batch_prepare.FINAL_RESUME_DIR = old_draft, old_final
    if not rows:
        raise RuntimeError("Shared resume pipeline returned no result.")
    return rows[0]


def _public(row: dict) -> dict:
    out = dict(row)
    out.pop("description", None)
    pdf = Path(str(row.get("pdf_path") or ""))
    out["resume_available"] = bool(row.get("pdf_path") and pdf.exists() and pdf.is_file())
    out["resume_url"] = f"/manual-resume/{row['key']}" if out["resume_available"] else None
    return out


def list_jobs() -> list[dict]:
    rows = [_public(x) for x in _load_state()["jobs"].values()]
    return sorted(rows, key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)


def add_links(value: list[str] | str) -> list[dict]:
    state = _load_state()
    out = []
    changed = False
    for url in _split_links(value):
        key = _key_for_url(url)
        if key in state["jobs"]:
            out.append(_public(state["jobs"][key]))
            continue
        now = _now()
        row = {
            "key": key, "external_id": key, "submitted_url": url, "url": url, "original_url": url,
            "company": "", "title": "", "location": "", "employment_type": None, "description": "",
            "source": "manual_link", "status": "PENDING", "application_status": "NOT_STARTED",
            "created_at": now, "updated_at": now, "error": None, "manual_filters_bypassed": True,
        }
        state["jobs"][key] = row
        out.append(_public(row))
        changed = True
    if changed:
        _save_state(state)
    return out


def _get(key: str) -> tuple[dict, dict]:
    state = _load_state()
    row = state["jobs"].get(key)
    if not row:
        raise HTTPException(404, "Manual job not found")
    return state, row


def process_job(key: str, force_refetch: bool = False) -> dict:
    state, row = _get(key)
    row["status"] = "FETCHING_JD" if force_refetch or not row.get("description_usable") else "GENERATING_RESUME"
    row["error"] = None
    row["updated_at"] = _now()
    state["jobs"][key] = row
    _save_state(state)
    try:
        if force_refetch or not row.get("description_usable") or not row.get("description"):
            raw = fetch_manual_job(row.get("submitted_url") or row.get("url"))
            if not force_refetch:
                for field in ("location", "employment_type"):
                    if row.get(field):
                        raw[field] = row[field]
        else:
            raw = {
                "external_id": key,
                "source": row.get("source") or "manual_link",
                "company_key": row.get("company") or "Unknown company",
                "company": row.get("company") or "Unknown company",
                "title": row.get("title") or "Job from supplied link",
                "description": row.get("description") or "",
                "location": row.get("location") or "",
                "employment_type": row.get("employment_type"),
                "url": row.get("url") or row.get("submitted_url"),
                "original_url": row.get("original_url") or row.get("url") or row.get("submitted_url"),
                "submitted_url": row.get("submitted_url"),
                "ats_provider": row.get("ats_provider"),
                "ats_identifier": row.get("ats_identifier"),
                "ats_label": row.get("ats_label"),
                "ats_tenant": row.get("ats_tenant"),
                "requisition_id": row.get("requisition_id"),
                "description_complete": bool(row.get("description_complete")),
                "description_usable": bool(row.get("description_usable")),
                "tailoring_mode": row.get("tailoring_mode") or "BASE_RESUME_CONSERVATIVE",
                "manual_link": True,
                "manual_filters_bypassed": True,
                "application_route": "MANUAL_LINK",
                "company_verified": row.get("company_verified", bool(_clean_company_label(row.get("company")))),
                "company_source": row.get("company_source") or "manual_state",
                "title_verified": row.get("title_verified", _has_verified_job_title(row.get("title"))),
            }
        print(
            "MANUAL JD QUALITY | company={} | company_source={} | title={} | title_verified={} | "
            "provider={} | metadata_source={} | jd_source={} | chars={} | complete={} | usable={}".format(
                raw.get("company") or "-",
                raw.get("company_source") or "-",
                raw.get("title") or "-",
                bool(raw.get("title_verified", _has_verified_job_title(raw.get("title")))),
                raw.get("ats_provider") or "-",
                raw.get("metadata_resolution_source") or "-",
                raw.get("jd_resolution_source") or raw.get("description_source") or "-",
                len(str(raw.get("description") or "")),
                bool(raw.get("description_complete")),
                bool(raw.get("description_usable")),
            ),
            flush=True,
        )

        if not raw.get("description_usable") or not str(raw.get("description") or "").strip():
            ignore_status = "IGNORED_NO_JD"
            ignore_reason = "No usable JD could be extracted."
        elif not _has_verified_job_title(raw.get("title")) or raw.get("title_verified") is False:
            ignore_status = "IGNORED_INCOMPLETE_JOB"
            ignore_reason = "The exact job title could not be verified from the supplied/resolved page."
        elif raw.get("company_verified") is False or _clean_company_label(raw.get("company")) in {"", "Company"}:
            ignore_status = "IGNORED_INCOMPLETE_JOB"
            ignore_reason = "The employer identity could not be verified from the supplied/resolved page."
        else:
            ignore_status = None
            ignore_reason = None

        if ignore_status:
            row.update({
                "status": ignore_status,
                "next_action": ignore_status,
                "company": _clean_company_label(raw.get("company")) or "",
                "title": _clean_job_title(raw.get("title")) or "",
                "description": "",
                "description_usable": bool(raw.get("description_usable")),
                "description_complete": bool(raw.get("description_complete")),
                "source": raw.get("source") or row.get("source") or "manual_link",
                "url": raw.get("url") or row.get("url"),
                "original_url": raw.get("original_url") or row.get("original_url"),
                "ats_provider": raw.get("ats_provider"),
                "ats_identifier": raw.get("ats_identifier"),
                "ats_label": raw.get("ats_label") or _ats_display(raw.get("ats_provider"), raw.get("ats_identifier"))[0],
                "ats_tenant": raw.get("ats_tenant") or _ats_display(raw.get("ats_provider"), raw.get("ats_identifier"))[1],
                "company_verified": bool(raw.get("company_verified")),
                "company_source": raw.get("company_source") or "unverified",
                "title_verified": bool(raw.get("title_verified")),
                "error": None,
                "ignore_reason": ignore_reason,
                "updated_at": _now(),
            })
            state = _load_state()
            if key in state["jobs"]:
                state["jobs"][key] = row
                _save_state(state)
            print(
                f"MANUAL IGNORE QUALITY | status={ignore_status} | company={row.get('company') or '-'} | "
                f"title={row.get('title') or '-'} | reason={ignore_reason}",
                flush=True,
            )
            return _public(row)

        # Full JD: tailor through the shared resume writer.
        # Partial but usable JD: use the uploaded master resume, unchanged in
        # content, via batch_prepare's existing master-rendering branch.
        # Missing JD or unverified company/title remain blocked above.
        raw["force_jd_tailoring"] = bool(raw.get("description_complete"))
        if not raw["force_jd_tailoring"]:
            raw["tailoring_mode"] = "BASE_RESUME_CONSERVATIVE"
            raw["resume_source_label"] = "MASTER_RESUME_FALLBACK"
            print(
                f"MANUAL MASTER FALLBACK | company={raw.get('company')} | title={raw.get('title')} "
                "| partial JD verified; no LLM tailoring",
                flush=True,
            )

        clean_company = _clean_company_label(raw.get("company_key") or raw.get("company")) or "Company"
        clean_title = _clean_job_title(raw.get("title")) or "Job opening"
        raw["company_key"] = clean_company
        raw["company"] = clean_company
        raw["title"] = clean_title
        row.update({
            "company": clean_company,
            "title": clean_title,
            "location": raw.get("location") or "",
            "employment_type": raw.get("employment_type"),
            "description": raw.get("description") or "",
            "source": raw.get("source") or "manual_link",
            "url": raw.get("url") or row.get("url"),
            "original_url": raw.get("original_url") or row.get("original_url"),
            "ats_provider": raw.get("ats_provider"),
            "ats_identifier": raw.get("ats_identifier"),
            "ats_label": raw.get("ats_label") or _ats_display(raw.get("ats_provider"), raw.get("ats_identifier"))[0],
            "ats_tenant": raw.get("ats_tenant") or _ats_display(raw.get("ats_provider"), raw.get("ats_identifier"))[1],
            "requisition_id": raw.get("requisition_id"),
            "company_verified": bool(raw.get("company_verified")),
            "company_source": raw.get("company_source") or "unverified",
            "title_verified": bool(raw.get("title_verified", _has_verified_job_title(raw.get("title")))),
            "description_complete": bool(raw.get("description_complete")),
            "description_usable": bool(raw.get("description_usable")),
            "tailoring_mode": raw.get("tailoring_mode"),
            "status": "GENERATING_RESUME",
            "updated_at": _now(),
        })
        state = _load_state()
        state["jobs"][key] = row
        _save_state(state)
        result = run_shared_resume_pipeline(raw)
        row.update({
            "status": result.get("next_action") or "HOLD_RESUME_ERROR",
            "next_action": result.get("next_action"),
            "resume_path": result.get("resume_path"),
            "pdf_path": result.get("pdf_path"),
            "resume_tailoring_policy": result.get("resume_tailoring_policy"),
            "resume_source_label": "MASTER_RESUME_FALLBACK" if not raw.get("description_complete") else "JD_TAILORED",
            "ats_audit": result.get("ats_audit"),
            "artifact_validation": result.get("artifact_validation"),
            "error": None if result.get("next_action") == "READY_TO_APPLY" else (result.get("ats_audit") or {}).get("error"),
            "updated_at": _now(),
        })
    except Exception as exc:
        row["status"] = "JD_FETCH_FAILED" if not row.get("description_usable") else "HOLD_RESUME_ERROR"
        row["error"] = str(exc)
        row["updated_at"] = _now()
    state = _load_state()
    if key in state["jobs"]:
        prior = state["jobs"][key]
        if prior.get("application_status") == "SUBMITTED_CONFIRMED":
            row["application_status"] = "SUBMITTED_CONFIRMED"
            row["submitted_at"] = prior.get("submitted_at")
        state["jobs"][key] = row
        _save_state(state)
    return _public(row)


def process_jobs(keys: list[str]) -> None:
    """Process one manual batch sequentially, with a volume-backed lock across deploy overlap."""
    with _BATCH_LOCK:
        lock_handle = None
        try:
            if fcntl is not None:
                MANUAL_DIR.mkdir(parents=True, exist_ok=True)
                lock_handle = (MANUAL_DIR / "batch.lock").open("a+", encoding="utf-8")
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            for key in keys:
                try:
                    _, row = _get(key)
                    if row.get("application_status") == "SUBMITTED_CONFIRMED" or row.get("status") == "READY_TO_APPLY":
                        continue
                    process_job(key)
                except Exception as exc:
                    print(f"MANUAL BACKGROUND ERROR {key}: {exc}", flush=True)
        finally:
            if lock_handle is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_handle.close()


def _company_is_aggregator(value: str | None) -> bool:
    low = _collapse_label(value).lower()
    return bool(low) and (
        low in _AGGREGATOR_NAMES
        or any(low == name or low.startswith(name + " ") for name in _AGGREGATOR_NAMES)
    )


def _recover_interrupted_jobs() -> list[str]:
    """Resume a manual batch after deploy/restart and retry the known stale-count failure."""
    state = _load_state()
    keys: list[str] = []
    changed = False
    for key, row in state["jobs"].items():
        if row.get("application_status") == "SUBMITTED_CONFIRMED":
            continue
        status = row.get("status")
        structural_retry = (
            status == "HOLD_RESUME_ERROR"
            and "must contain exactly" in str(row.get("error") or "").lower()
        )
        audit = row.get("ats_audit") or {}
        audit_structure_retry = (
            status == "HOLD_ATS_REVIEW"
            and (
                "structure" in (audit.get("blocking_quality_gates") or [])
                or (audit.get("quality_gates") or {}).get("structure") is False
            )
        )
        bad_ready_company = status == "READY_TO_APPLY" and _company_is_aggregator(row.get("company"))
        if bad_ready_company:
            row.update({
                "company": "",
                "description": "",
                "description_usable": False,
                "description_complete": False,
                "status": "PENDING",
                "next_action": None,
                "resume_path": None,
                "pdf_path": None,
                "error": None,
                "updated_at": _now(),
            })
            changed = True
        if status in _PROCESSING_STATUSES or structural_retry or audit_structure_retry or bad_ready_company:
            keys.append(key)
    if changed:
        _save_state(state)
    if keys:
        print(f"MANUAL STARTUP RECOVERY | jobs={len(keys)}", flush=True)
        threading.Thread(
            target=process_jobs,
            args=(keys,),
            daemon=True,
            name="manual-job-recovery",
        ).start()
    return keys


def reset_manual_state() -> int:
    """Delete only manual-link state/work/resumes and recreate an empty manual queue."""
    previous = len(_load_state().get("jobs") or {})
    shutil.rmtree(MANUAL_DIR, ignore_errors=True)
    _save_state({"version": 1, "jobs": {}})
    return previous


def _pause_active_manual_jobs() -> int:
    state = _load_state()
    paused = 0
    for row in state["jobs"].values():
        if row.get("status") in _PROCESSING_STATUSES:
            row["status"] = "PAUSED"
            row["error"] = None
            row["updated_at"] = _now()
            paused += 1
    if paused:
        _save_state(state)
    return paused


@router.on_event("startup")
def _startup_recover_manual_jobs() -> None:
    if os.getenv("MANUAL_RESET_ON_START", "").strip().lower() in {"1", "true", "yes", "on"}:
        deleted = reset_manual_state()
        print(f"MANUAL RESET COMPLETE | deleted={deleted}", flush=True)
        return
    if os.getenv("MANUAL_RECOVERY_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        paused = _pause_active_manual_jobs()
        print(f"MANUAL STARTUP RECOVERY DISABLED | paused={paused}", flush=True)
        return
    _recover_interrupted_jobs()



def edit_job(key: str, body: ManualEdit) -> dict:
    state, row = _get(key)
    data = body.model_dump(exclude_unset=True)
    description_supplied = "description" in data
    if data.get("url"):
        normalized = _normalize_url(data["url"])
        data.update({
            "url": normalized, "submitted_url": normalized, "original_url": normalized,
            "description": "", "description_usable": False, "description_complete": False, "status": "PENDING",
        })
    if description_supplied:
        desc = (data.get("description") or "").strip()
        complete = _looks_like_complete_jd(desc, row.get("source") or "manual_link")
        usable = complete or _looks_like_usable_jd(desc, row.get("source") or "manual_link")
        data.update({
            "description": desc, "description_complete": bool(complete), "description_usable": bool(usable),
            "tailoring_mode": "FULL_JD" if complete else "BASE_RESUME_CONSERVATIVE",
            "status": "JD_READY" if usable else "JD_FETCH_FAILED",
        })
    for field in ("company", "title", "location", "employment_type"):
        if field in data and data[field] is not None:
            data[field] = str(data[field]).strip()
    for field in ("resume_path", "pdf_path", "ats_audit", "artifact_validation", "resume_tailoring_policy", "next_action"):
        row.pop(field, None)
    row.update(data)
    row["error"] = None
    row["updated_at"] = _now()
    state["jobs"][key] = row
    _save_state(state)
    return _public(row)


def delete_job(key: str) -> dict:
    state, row = _get(key)
    state["jobs"].pop(key, None)
    _save_state(state)
    shutil.rmtree(WORK_DIR / _safe_key(key), ignore_errors=True)
    shutil.rmtree(ARTIFACT_DIR / _safe_key(key), ignore_errors=True)
    return {"ok": True, "deleted": True, "key": key, "company": row.get("company"), "title": row.get("title")}


def confirm_submitted(key: str) -> dict:
    state, row = _get(key)
    row["application_status"] = "SUBMITTED_CONFIRMED"
    row["submitted_at"] = _now()
    row["updated_at"] = row["submitted_at"]
    state["jobs"][key] = row
    _save_state(state)
    return _public(row)


@router.get("/api/manual-links")
def api_list():
    all_rows = list_jobs()
    visible = [
        x for x in all_rows
        if x.get("status") == "READY_TO_APPLY"
        or x.get("application_status") == "SUBMITTED_CONFIRMED"
    ]
    processing_rows = [x for x in all_rows if _is_processing(x)]
    current = processing_rows[-1] if processing_rows else None
    terminal_hidden = [
        x for x in all_rows
        if x not in visible and not _is_processing(x)
    ]
    return {
        "jobs": visible,
        "current": current,
        "processing_jobs": processing_rows,
        "attention": terminal_hidden[:20:],
        "counts": {
            "total": len(all_rows),
            "ready": sum(
                x.get("status") == "READY_TO_APPLY"
                and x.get("application_status") != "SUBMITTED_CONFIRMED"
                for x in visible
            ),
            "applied": sum(x.get("application_status") == "SUBMITTED_CONFIRMED" for x in visible),
            "processing": len(processing_rows),
            "completed": len(visible),
            "hidden_terminal": len(terminal_hidden),
        },
    }


@router.post("/api/manual-links")
def api_add(body: LinksInput, background_tasks: BackgroundTasks):
    try:
        jobs = add_links(body.links)
        keys = [
            x["key"] for x in jobs
            if x.get("application_status") != "SUBMITTED_CONFIRMED"
            and x.get("status") != "READY_TO_APPLY"
        ]
        if keys:
            background_tasks.add_task(process_jobs, keys)
        return {"ok": True, "jobs": jobs, "queued": len(keys)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/manual-links/{key:path}/process")
def api_process(key: str, force_refetch: bool = False):
    return {"ok": True, "job": process_job(key, force_refetch)}


@router.post("/api/manual-links/{key:path}/retry")
def api_retry(key: str, background_tasks: BackgroundTasks):
    state, row = _get(key)
    if row.get("application_status") == "SUBMITTED_CONFIRMED":
        return {"ok": True, "job": _public(row), "queued": False}
    row.update({
        "status": "PENDING",
        "next_action": None,
        "description": "",
        "description_usable": False,
        "description_complete": False,
        "tailoring_mode": None,
        "error": None,
        "ignore_reason": None,
        "resume_path": None,
        "pdf_path": None,
        "ats_audit": None,
        "artifact_validation": None,
        "resume_tailoring_policy": None,
        "updated_at": _now(),
    })
    state["jobs"][key] = row
    _save_state(state)
    background_tasks.add_task(process_jobs, [key])
    return {"ok": True, "job": _public(row), "queued": True}


@router.patch("/api/manual-links/{key:path}")
def api_edit(key: str, body: ManualEdit):
    try:
        return {"ok": True, "job": edit_job(key, body)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/manual-links/{key:path}")
def api_delete(key: str):
    return delete_job(key)


@router.post("/api/manual-links/{key:path}/confirm-submitted")
def api_confirm(key: str):
    return {"ok": True, "job": confirm_submitted(key)}


@router.get("/manual-resume/{key:path}")
def manual_resume(key: str):
    _, row = _get(key)
    path = Path(str(row.get("pdf_path") or ""))
    if not row.get("pdf_path") or not path.exists() or not path.is_file() or path.suffix.lower() != ".pdf":
        raise HTTPException(404, "Resume not available")
    allowed, resolved = ARTIFACT_DIR.resolve(), path.resolve()
    if allowed != resolved and allowed not in resolved.parents:
        raise HTTPException(403, "Invalid manual resume path")
    return FileResponse(resolved, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{resolved.name}"'})


MANUAL_PAGE = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Manual Job Links · Auto Apply</title>
<style>
:root{
  --bg:#07111f;--sidebar:#0a1625;--panel:#0b1726;--panel2:#0e1c2e;
  --line:#21344a;--line2:#2a4058;--text:#edf3fb;--muted:#91a4bc;
  --blue:#2f73df;--blue2:#4388f4;--green:#55dea5;--amber:#f1bd62;--red:#ff8f91;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14px}
button,textarea{font:inherit}
.app{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100vh}
.side{background:var(--sidebar);border-right:1px solid #1a2a3d;padding:22px 14px;position:sticky;top:0;height:100vh}
.brand{font-size:20px;font-weight:850;padding:0 8px 24px;letter-spacing:-.3px}
.brand small{display:block;color:var(--muted);font-size:11px;font-weight:500;margin-top:4px;letter-spacing:0}
.nav{display:grid;gap:6px}
.nav a{display:flex;align-items:center;gap:9px;padding:11px 12px;border-radius:8px;color:#c7d2e2;text-decoration:none;font-size:13px;font-weight:650}
.nav a:hover,.nav .active{background:#173967;color:#fff}
.main{padding:26px clamp(18px,2.4vw,34px) 42px;min-width:0;max-width:1760px;width:100%;margin:0 auto}
.top{display:flex;justify-content:space-between;gap:22px;align-items:flex-start;margin-bottom:18px}
.eyebrow{display:inline-flex;align-items:center;gap:7px;color:#8ab8ff;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px}
.eyebrow:before{content:"";width:7px;height:7px;border-radius:50%;background:#4d91ff;box-shadow:0 0 0 4px rgba(77,145,255,.12)}
.top h1{font-size:27px;line-height:1.15;letter-spacing:-.5px;margin:0 0 7px}
.subtitle{color:#9fb0c7;font-size:13px;line-height:1.55;max-width:820px}
.flowTag{flex:0 0 auto;border:1px solid #29496d;background:#0f2035;color:#a9c9f5;padding:8px 11px;border-radius:999px;font-size:11px;font-weight:800}
.entry{background:linear-gradient(180deg,#0d1a2b 0%,#0b1726 100%);border:1px solid var(--line);border-radius:13px;padding:17px;margin-bottom:14px;box-shadow:0 12px 32px rgba(0,0,0,.13)}
.entryTop{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:10px}
.entryTitle{font-size:13px;font-weight:800}
.entryHint{font-size:11px;color:#7f93ac}
textarea{width:100%;min-height:132px;background:#071321;border:1px solid #2a4058;color:#eef4fc;border-radius:9px;padding:13px 14px;resize:vertical;outline:none;line-height:1.5;transition:border .15s,box-shadow .15s}
textarea::placeholder{color:#6f8299}
textarea:focus{border-color:#4b8bf0;box-shadow:0 0 0 3px rgba(75,139,240,.13)}
.toolbar{display:flex;gap:8px;align-items:center;margin-top:11px;flex-wrap:wrap}
.btn{border:1px solid #30465f;background:#13253a;color:#dce7f5;padding:8px 11px;border-radius:7px;font-size:11px;font-weight:800;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;transition:transform .12s,background .12s,border .12s}
.btn:hover{background:#19304a;border-color:#416080}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--blue);border-color:var(--blue2);color:#fff}
.btn.primary:hover{background:#377de9}
.btn.danger{color:#ffaaa9}
.btn:disabled{opacity:.62;cursor:default;transform:none}
.processingPill{display:none;align-items:center;gap:7px;margin-left:auto;padding:7px 10px;border-radius:999px;background:#2d2850;border:1px solid #463b77;color:#d6c9ff;font-size:11px;font-weight:800}
.processingPill.on{display:inline-flex}
.dot{width:7px;height:7px;border-radius:50%;background:#c6a8ff;animation:pulse 1.1s infinite}
@keyframes pulse{50%{opacity:.3}}
.pipeline{display:flex;align-items:center;gap:7px;margin-top:12px;padding-top:12px;border-top:1px solid #182a3d;color:#8295ad;font-size:10px;font-weight:750;overflow:auto;white-space:nowrap}
.step{display:inline-flex;align-items:center;gap:6px}
.step i{font-style:normal;display:grid;place-items:center;width:18px;height:18px;border-radius:50%;background:#142943;color:#8ab8ff;font-size:9px}
.arrow{color:#40566f}
.current{display:none;margin-top:11px;padding:10px 12px;border:1px solid #433971;border-radius:8px;background:#17152a;color:#cbbcf0;font-size:11px;font-weight:700}
.current.on{display:block}
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}
.stat{background:#0d1a2b;border:1px solid #263950;border-radius:10px;padding:13px 14px;min-height:76px}
.stat span{color:#95a7bd;font-size:10px;text-transform:uppercase;letter-spacing:.055em;font-weight:800}
.stat b{display:block;font-size:23px;line-height:1;margin-top:9px;letter-spacing:-.4px}
.stat.attention b{color:var(--amber)}
.tabsbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:0 0 14px}
.tabs{display:flex;gap:7px;overflow:auto;min-width:0}
.tab{border:1px solid #263950;background:#0d1a2b;color:#aebdd0;border-radius:8px;padding:9px 13px;white-space:nowrap;cursor:pointer;font-weight:700}
.tab.active{background:#2684ff;color:#fff;border-color:#2684ff}
.filters{display:flex;gap:7px;flex:0 0 auto}
.filters input{background:#0d1a2b;border:1px solid #263950;color:#dce6f4;border-radius:8px;padding:9px 11px;min-width:145px}
.section{background:var(--panel);border:1px solid var(--line);border-radius:11px;margin-bottom:14px;overflow:visible}
.section.hidden{display:none}
.sectionHead{padding:14px 16px;border-bottom:1px solid #1d3044;font-weight:800;display:flex;justify-content:space-between;align-items:center;gap:12px}
.sectionHead small,.count{color:#8fa0b8;font-size:11px;font-weight:650}
.row{display:grid;grid-template-columns:minmax(260px,1fr) 130px 180px minmax(340px,380px);column-gap:18px;align-items:center;padding:14px 16px;border-bottom:1px solid #17283a;font-size:12px}
.row:last-child{border-bottom:0}
.row:not(.head):hover{background:#0e1c2d}
.row.head{background:#101f31;color:#9fb0c7;font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding-top:12px;padding-bottom:12px;font-weight:500}
.jobInfo,.statusCell,.row>div{min-width:0}
.title{font-weight:800;color:#f3f7fc;font-size:13px;line-height:1.3;margin-bottom:6px;overflow-wrap:anywhere}
.meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:#8fa0b8;font-size:11px;line-height:1.4}
.meta .company{color:#b9c8da;font-weight:700;margin:0}
.metaDot{color:#40536b}
.created{color:#a9b8ca;line-height:1.35;white-space:nowrap}
.badge{display:inline-flex;align-items:center;justify-content:center;padding:5px 9px;border-radius:999px;font-size:10px;font-weight:800;white-space:nowrap}
.ready{background:#0d4637;color:#62e5b0}
.applied{background:#173b69;color:#74b4ff}
.warn{background:#493a1d;color:#ffd48b}
.actions{display:grid;grid-template-columns:82px 94px 106px 36px;gap:6px;align-items:center;justify-content:end;width:100%}
.actions>.btn,.actions>.badge{height:32px;display:flex;align-items:center;justify-content:center;padding:0 8px}
.actions .primary{min-width:0}
.actions .applied{margin:0}
.menuWrap{position:relative;width:36px}
.actions .menuWrap>.btn{width:36px;height:32px;padding:0}
.menu{display:none;position:absolute;right:0;top:37px;background:#102033;border:1px solid #2a4058;padding:6px;border-radius:8px;z-index:20;box-shadow:0 10px 30px #0008}
.menu.open{display:block}
.deleteBtn{color:#ff8f91}
.empty{padding:34px 20px;text-align:center;color:#7589a1}
.empty b{display:block;color:#b9c7d7;font-size:12px;margin-bottom:5px}
.attentionRow{grid-template-columns:minmax(220px,.9fr) 145px minmax(220px,1.15fr) 270px}
.attentionRow .actions{display:grid;grid-template-columns:88px 82px 74px;gap:6px;justify-content:end;width:100%;white-space:nowrap}
.attentionRow .actions .btn{min-width:0;width:100%;height:32px;padding:0 7px}
.reason{color:#c7b792;font-size:10px;line-height:1.5;overflow-wrap:anywhere;word-break:break-word}
.footerNote{color:#72869d;font-size:10px;text-align:right;margin-top:2px}
@media(max-width:1200px){
  .app{grid-template-columns:180px minmax(0,1fr)}
  .row{grid-template-columns:minmax(220px,1fr) 120px 155px 330px;column-gap:12px}
  .actions{grid-template-columns:76px 88px 100px 34px;gap:5px}
  .actions .menuWrap>.btn,.menuWrap{width:34px}
  .btn{font-size:9.5px}
  .attentionRow{grid-template-columns:minmax(200px,.9fr) 125px minmax(200px,1fr) 250px}
  .attentionRow .actions{grid-template-columns:82px 76px 70px;gap:5px}
}
@media(max-width:980px){
  .app{grid-template-columns:1fr}.side{height:auto;position:relative;border-right:0;border-bottom:1px solid #1a2a3d;padding:11px 14px}
  .brand{padding:0 4px 10px;font-size:18px}.nav{display:flex;gap:6px;overflow-x:auto}.nav a{flex:0 0 auto;padding:8px 10px}.main{padding:16px}
  .tabsbar{align-items:stretch;flex-direction:column}.filters{width:100%}.filters>*{flex:1}
  .row{grid-template-columns:minmax(200px,1fr) 120px 150px}.row.head>div:last-child{display:none}.row>div:last-child{grid-column:1/-1}
  .actions{justify-content:start;width:auto;grid-template-columns:82px 94px 106px 36px;margin-top:2px}
  .attentionRow{grid-template-columns:1fr 140px}.attentionRow>div:nth-child(3),.attentionRow>div:last-child{grid-column:1/-1}.attentionRow .actions{justify-content:flex-start;grid-template-columns:88px 82px 74px;width:max-content;max-width:100%}
}
@media(max-width:650px){
  .main{padding:10px}.top{display:block}.flowTag{display:none}.top h1{font-size:20px}
  .entryTop{display:block}.entryHint{margin-top:4px}.stats{grid-template-columns:repeat(2,1fr)}
  .filters{display:grid;grid-template-columns:1fr}.tabs{width:100%}
  .stat:last-child{grid-column:1/-1}.row,.attentionRow{display:block}.row.head{display:none}.row>div{margin-bottom:7px}.row>div:last-child{margin-bottom:0}
  .title{font-size:14px}.actions{margin-top:9px;grid-template-columns:repeat(2,minmax(0,1fr));width:100%}
  .actions .menuWrap{width:100%}.actions .menuWrap>.btn{width:100%}.actions>.btn,.actions>.badge,.actions .menuWrap>.btn{height:36px}
  .menu{left:0;right:auto;top:40px}.attentionRow .actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));width:100%}.processingPill{margin-left:0}.pipeline{padding-bottom:2px}.footerNote{text-align:left}
}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">💼 Auto Apply<small>Job Application Manager</small></div>
    <nav class="nav">
      <a href="/">⌂ <span>Job Discovery</span></a>
      <a class="active" href="/manual-links">🔗 <span>Manual Job Links</span></a>
      <a href="/pasted-jds">📝 <span>Paste Job Description</span></a>
    </nav>
  </aside>
  <main class="main">
    <header class="top">
      <div>
        <div class="eyebrow">Direct-link workflow</div>
        <h1>Manual Job Links</h1>
        <div class="subtitle">Paste employer or ATS job links. The agent resolves the exact posting, verifies company and title, extracts the full JD, then uses the same production resume pipeline as Job Discovery.</div>
      </div>
      <div class="flowTag">No discovery filters · Same resume rules</div>
    </header>

    <section class="entry">
      <div class="entryTop">
        <div class="entryTitle">Add job links</div>
        <div class="entryHint">One URL per line · processed in submission order</div>
      </div>
      <textarea id="links" spellcheck="false" placeholder="https://company.wd5.myworkdayjobs.com/...&#10;https://boards.greenhouse.io/...&#10;https://jobs.lever.co/..."></textarea>
      <div class="toolbar">
        <button class="btn primary" id="addBtn">＋ Add & Process</button>
        <button class="btn" id="refreshBtn">↻ Refresh</button>
        <span class="processingPill" id="processingPill"><span class="dot"></span><span id="processingText">Processing…</span></span>
      </div>
      <div class="current" id="currentJob"></div>
      <div class="pipeline">
        <span class="step"><i>1</i> Detect ATS</span><span class="arrow">→</span>
        <span class="step"><i>2</i> Resolve exact job</span><span class="arrow">→</span>
        <span class="step"><i>3</i> Extract full JD</span><span class="arrow">→</span>
        <span class="step"><i>4</i> Tailor + audit resume</span><span class="arrow">→</span>
        <span class="step"><i>5</i> Ready to apply</span>
      </div>
    </section>

    <section class="stats">
      <div class="stat"><span>Submitted</span><b id="total">0</b></div>
      <div class="stat"><span>Ready</span><b id="ready">0</b></div>
      <div class="stat"><span>Applied</span><b id="applied">0</b></div>
      <div class="stat"><span>Processing</span><b id="processing">0</b></div>
      <div class="stat attention"><span>Needs attention</span><b id="attentionCount">0</b></div>
    </section>

    <div class="tabsbar">
      <div class="tabs" id="dateTabs"></div>
      <div class="filters"><input id="datePicker" type="date" aria-label="Select manual job date"></div>
    </div>

    <section class="section" id="attentionSection">
      <div class="sectionHead"><span>Needs Attention</span><small>Failed or incomplete links stay visible here instead of disappearing</small></div>
      <div class="row head attentionRow"><div>Job</div><div>Status</div><div>What happened</div><div>Actions</div></div>
      <div id="attentionJobs"></div>
    </section>

    <section class="section">
      <div class="sectionHead"><span id="readyTitle">Ready to Apply</span><span class="count" id="readyCount">Showing 0 jobs</span></div>
      <div class="row head"><div>Job</div><div>Status</div><div>Applied Date</div><div>Actions</div></div>
      <div id="jobs"></div>
    </section>
    <div class="footerNote">Manual links bypass discovery only. ATS resolution, JD quality gates, resume tailoring, validation, and formatting remain shared with production.</div>
  </main>
</div>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let rows=[],attentionRows=[],processingRows=[],selectedDate=null;
function nice(v){let d=new Date(v);return!v||isNaN(d)?"—":d.toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}
function dateKey(v){
  if(!v)return "";
  let d=v instanceof Date?v:new Date(v);if(isNaN(d))return String(v).slice(0,10);
  let parts=new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(d);
  let o=Object.fromEntries(parts.map(x=>[x.type,x.value]));return o.year+"-"+o.month+"-"+o.day;
}
function labelDate(k){let d=new Date(k+"T12:00:00");return d.toLocaleDateString([],{month:"short",day:"numeric"})}
function rowDate(r){return dateKey(r.created_at||r.updated_at||r.submitted_at)}
function inSelectedDate(r){return !selectedDate||rowDate(r)===selectedDate}
function renderDateTabs(){
  let today=dateKey(new Date());if(selectedDate===null)selectedDate=today;
  dateTabs.innerHTML='<button class="tab '+(selectedDate===today?"active":"")+'" data-date="'+today+'">Today</button><button class="tab '+(!selectedDate?"active":"")+'" data-all-dates="1">All Dates</button><button class="tab" data-prev="1">← Previous</button><button class="tab" data-next="1">Next →</button>';
  datePicker.value=selectedDate||"";
}
function ats(r){let label=r.ats_label||((r.ats_provider||"Direct employer page").replaceAll("_"," "));return r.ats_tenant?label+" · "+r.ats_tenant:label}
function labelStatus(v){return String(v||"Needs attention").replaceAll("_"," ").toLowerCase().replace(/\b\w/g,m=>m.toUpperCase())}
function reason(r){return r.ignore_reason||r.error||((r.status||"").includes("INCOMPLETE")?"The exact job metadata or full JD could not be verified.":"This link did not complete the production quality gates.")}
function syncBatch(d){
  let n=Number(d.counts?.processing||0),active=n>0;
  processing.textContent=n;addBtn.disabled=active;addBtn.textContent=active?"Processing…":"＋ Add & Process";
  processingPill.classList.toggle("on",active);
  processingText.textContent=active?"Processing · "+n+" remaining":"Processing…";
  let cur=d.current;
  if(active&&cur){
    let stage=(cur.status||"PROCESSING").replaceAll("_"," ").toLowerCase();
    let who=[cur.company,cur.title].filter(Boolean).join(" · ")||"submitted link";
    currentJob.textContent="Currently "+stage+": "+who;
    currentJob.classList.add("on");
  }else{currentJob.classList.remove("on");currentJob.textContent=""}
}
async function load(){
  try{
    let d=await fetch("/api/manual-links",{cache:"no-store"}).then(r=>r.json());
    rows=d.jobs||[];attentionRows=d.attention||[];processingRows=d.processing_jobs||[];
    renderDateTabs();syncBatch(d);render();renderAttention();
  }catch(e){}
}
function render(){
  let visibleRows=rows.filter(inSelectedDate),visibleAttention=attentionRows.filter(inSelectedDate),visibleProcessing=processingRows.filter(inSelectedDate);
  let appliedRows=visibleRows.filter(r=>r.application_status==="SUBMITTED_CONFIRMED");
  let readyRows=visibleRows.filter(r=>r.application_status!=="SUBMITTED_CONFIRMED");
  total.textContent=visibleRows.length+visibleAttention.length+visibleProcessing.length;
  ready.textContent=readyRows.length;applied.textContent=appliedRows.length;processing.textContent=visibleProcessing.length;attentionCount.textContent=visibleAttention.length;
  readyTitle.textContent=selectedDate?("Ready to Apply — "+labelDate(selectedDate)):"Ready to Apply — All Dates";
  readyCount.textContent="Showing "+visibleRows.length+" jobs";
  jobs.innerHTML=visibleRows.map(r=>{
    let applied=r.application_status==="SUBMITTED_CONFIRMED";
    let meta=[ats(r),r.requisition_id?("Req "+r.requisition_id):"Manual link"].filter(Boolean);
    return '<div class="row"><div class="jobInfo"><div class="title">'+esc(r.title||"Job opening")+'</div><div class="meta"><span class="company">'+esc(r.company||"Company")+'</span><span class="metaDot">•</span><span>'+esc(meta[0]||"Direct employer page")+'</span>'+(meta[1]?'<span class="metaDot">•</span><span>'+esc(meta[1])+'</span>':'')+'</div><div class="meta" style="margin-top:5px"><span>Updated '+esc(nice(r.updated_at))+'</span></div></div><div class="statusCell"><span class="badge '+(applied?'applied':'ready')+'">'+(applied?'Applied':'Ready to apply')+'</span></div><div class="created">'+(applied?nice(r.submitted_at||r.updated_at):"—")+'</div><div class="actions">'+(r.url?'<a class="btn primary" href="'+esc(r.url)+'" target="_blank" rel="noopener">Open Job</a>':'<span></span>')+(r.resume_url?'<a class="btn" href="'+esc(r.resume_url)+'" target="_blank" rel="noopener">View Resume</a>':'<span></span>')+(!applied?'<button class="btn" data-applied="'+esc(r.key)+'">✓ Mark Applied</button>':'<span class="badge applied">✓ Applied</span>')+'<div class="menuWrap"><button class="btn" data-menu="1">•••</button><div class="menu"><button class="btn deleteBtn" data-delete="'+esc(r.key)+'">Delete</button></div></div></div></div>';
  }).join("")||'<div class="empty"><b>No completed resumes yet</b>Ready jobs will appear here automatically after extraction, tailoring, and validation finish.</div>';
}
function renderAttention(){
  let visibleAttention=attentionRows.filter(inSelectedDate);
  attentionSection.classList.toggle("hidden",visibleAttention.length===0);
  attentionJobs.innerHTML=visibleAttention.map(r=>{
    return '<div class="row attentionRow"><div><div class="title">'+esc(r.title||"Unresolved job")+'</div><div class="company">'+esc(r.company||"Company not verified")+'</div><div class="meta">'+esc(ats(r))+' · '+nice(r.updated_at)+'</div></div><div><span class="badge warn">'+esc(labelStatus(r.status))+'</span></div><div class="reason">'+esc(reason(r))+'</div><div class="actions">'+(r.url?'<a class="btn" href="'+esc(r.url)+'" target="_blank" rel="noopener">Open Job</a>':'')+'<button class="btn primary" data-retry="'+esc(r.key)+'">↻ Retry</button><button class="btn danger" data-delete="'+esc(r.key)+'">Delete</button></div></div>';
  }).join("");
}
addBtn.onclick=async()=>{
  let value=links.value.trim();if(!value||addBtn.disabled)return;
  addBtn.disabled=true;addBtn.textContent="Processing…";processingPill.classList.add("on");processingText.textContent="Starting…";
  try{
    let r=await fetch("/api/manual-links",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({links:value})});
    let d=await r.json();if(!r.ok)throw new Error(d.detail||"Could not add links");
    links.value="";await load();
  }catch(e){alert(e.message);addBtn.disabled=false;addBtn.textContent="＋ Add & Process";processingPill.classList.remove("on")}
};
refreshBtn.onclick=load;
document.addEventListener("click",async e=>{
  let menu=e.target.closest("[data-menu]");
  if(menu){menu.nextElementSibling.classList.toggle("open");return}
  if(!e.target.closest(".menuWrap"))document.querySelectorAll(".menu.open").forEach(x=>x.classList.remove("open"));
  let a=e.target.closest("[data-applied]");
  if(a){if(confirm("Mark this application as Applied?")){await fetch("/api/manual-links/"+encodeURIComponent(a.dataset.applied)+"/confirm-submitted",{method:"POST"});await load()}return}
  let retry=e.target.closest("[data-retry]");
  if(retry){
    retry.disabled=true;retry.textContent="Retrying…";
    try{
      let response=await fetch("/api/manual-links/"+encodeURIComponent(retry.dataset.retry)+"/retry",{method:"POST"});
      let payload=await response.json();
      if(!response.ok)throw new Error(payload.detail||"Retry failed");
      await load();
    }catch(err){alert(err.message);retry.disabled=false;retry.textContent="↻ Retry"}
    return;
  }
  let d=e.target.closest("[data-delete]");
  if(d){if(confirm("Delete only this manual job and its generated resume?")){await fetch("/api/manual-links/"+encodeURIComponent(d.dataset.delete),{method:"DELETE"});await load()}}
});
dateTabs.addEventListener("click",e=>{
  let all=e.target.closest("[data-all-dates]");
  if(all){selectedDate="";renderDateTabs();render();renderAttention();return}
  let prev=e.target.closest("[data-prev]"),next=e.target.closest("[data-next]");
  if(prev||next){
    let base=selectedDate||dateKey(new Date()),d=new Date(base+"T12:00:00");
    d.setDate(d.getDate()+(next?1:-1));
    selectedDate=d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
    renderDateTabs();render();renderAttention();return;
  }
  let b=e.target.closest("[data-date]");
  if(b){selectedDate=b.dataset.date;renderDateTabs();render();renderAttention()}
});
datePicker.addEventListener("change",()=>{selectedDate=datePicker.value||"";renderDateTabs();render();renderAttention()});
load();setInterval(load,3000);
</script>
</body>
</html>'''

@router.get("/manual-links", response_class=HTMLResponse)
def manual_page():
    return HTMLResponse(MANUAL_PAGE)
