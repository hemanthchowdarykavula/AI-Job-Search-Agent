"""HiringCafe official read-only MCP discovery adapter.

Respect the documented conversational-rate limits: at most one small search
per cycle; never scrape HiringCafe's internal/private website endpoints.
All leads remain discovery-only until official employer/ATS verification.
"""
from __future__ import annotations
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.sources.mcp_jobs import call_tool, rows_from_payload

ENDPOINT = "https://hiringcafe.com/api/mcp"
SEARCH = "data engineer"
MAX_RESULTS = 20


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _posted(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _company(row):
    co = row.get("company") or row.get("company_name") or row.get("companyName")
    if isinstance(co, dict):
        return _text(co.get("name") or co.get("company_name"))
    return _text(co) or "Unknown"


def _location(row):
    value = row.get("location") or row.get("job_location") or row.get("location_name")
    if isinstance(value, dict):
        value = ", ".join(str(value[k]) for k in ("city", "state", "country") if value.get(k))
    if isinstance(value, list):
        value = " | ".join(map(str, value))
    return _text(value)


def _safe_url(value):
    url = str(value or "").strip()
    if not url.startswith(("https://", "http://")):
        return ""
    host = urlparse(url).hostname or ""
    if not host or host in {"localhost", "127.0.0.1"}:
        return ""
    return url


def normalize(row: dict) -> dict | None:
    title = _text(row.get("title") or row.get("job_title") or row.get("jobTitle"))
    if not title or not any(x in title.lower() for x in ("data engineer", "data platform", "analytics engineer", "etl engineer", "data infrastructure")):
        return None
    url = _safe_url(row.get("url") or row.get("job_url") or row.get("jobUrl")
                    or row.get("link") or row.get("hiringcafe_url"))
    if not url:
        return None
    company = _company(row)
    ident = row.get("id") or row.get("job_id") or row.get("jobId") or url
    guid = hashlib.sha1(str(ident).encode()).hexdigest()[:20]
    posted = _posted(row.get("date_posted") or row.get("posted_at") or row.get("datePosted") or row.get("created_at"))
    description = _text(row.get("description") or row.get("snippet") or row.get("summary"))
    out = {
        "external_id": f"hiringcafe:{guid}", "source": "hiringcafe",
        "company_key": company, "title": title, "location": _location(row),
        "employment_type": row.get("employment_type") or row.get("employmentType"),
        "url": url, "original_url": url, "description": description,
        "description_complete": False, "discovery_only": True,
        "authoritative_source": False, "portal_source": "hiringcafe",
        "posted_on": posted,
    }
    direct = _safe_url(row.get("employer_apply_url") or row.get("external_apply_url"))
    if direct:
        out["external_apply_url"] = direct
    return out


def fetch_jobs(hours: float = 24) -> list[dict]:
    """One bounded official search per production cycle, not a bulk job scrape."""
    args = {
        "search": SEARCH, "location": "United States",
        "employment_types": ["Full Time", "Contract"],
        "posted_within_days": max(1, min(7, int((float(hours) + 23) // 24))),
        "limit": MAX_RESULTS,
    }
    payload = call_tool(ENDPOINT, "search_jobs", args, retries=1)
    # Some MCP tools put the array under an explicit wrapper.
    rows = rows_from_payload(payload)
    if not rows and isinstance(payload, dict):
        rows = rows_from_payload(payload.get("result"))
    if isinstance(payload, dict) and payload.get("isError"):
        raise RuntimeError("HiringCafe official MCP returned an error")
    result = {}
    for row in rows:
        item = normalize(row)
        if item:
            result[item["external_id"]] = item
    return list(result.values())
