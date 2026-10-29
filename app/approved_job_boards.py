"""Five user-approved job boards: supported manual application destinations.

These are *board-verified listings*, NOT verified employer/ATS postings.
Never relabel a board timestamp as an employer posting timestamp.
Other aggregators (including Dice) retain the existing employer ATS gate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import re

APPROVED_BOARD_HOSTS = {
    "hiringcafe": ("hiringcafe.com",),
    "welcome_to_the_jungle": ("welcometothejungle.com",),
    "himalayas": ("himalayas.app",),
    "yc_jobs": ("ycombinator.com", "workatastartup.com"),
    "wellfound": ("wellfound.com",),
}
BOARD_APPLICATION_ROUTE = "JOB_BOARD_MANUAL"


def approved_board(source: str | None) -> bool:
    return str(source or "").lower() in APPROVED_BOARD_HOSTS


def board_url_is_valid(source: str | None, url: str | None) -> bool:
    if not approved_board(source) or not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or p.username or p.password:
            return False
        if p.port not in (None, 443):
            return False
        return any(host == domain or host.endswith("." + domain)
                   for domain in APPROVED_BOARD_HOSTS[source.lower()])
    except ValueError:
        return False


def board_application_url(row: dict) -> str:
    source = str(row.get("source") or "").lower()
    # Himalayas API terms explicitly require exactly applicationLink.
    candidates = (
        [row.get("source_apply_url"), row.get("applicationLink"), row.get("url"), row.get("original_url")]
        if source == "himalayas"
        else [row.get("board_url"), row.get("aggregator_url"), row.get("url"), row.get("original_url")]
    )
    return next((u for u in candidates if board_url_is_valid(source, u)), "")


def himalayas_open_details(job: dict, timeout: int = 15) -> tuple[bool | None, str, dict]:
    """Confirm one job through Himalayas' official single-job API (200/404/410).

    Never scrape their protected browser pages or treat 403/429 as job closure.
    """
    guid = job.get("guid") or job.get("url") or job.get("original_url") or ""
    if not board_url_is_valid("himalayas", guid):
        return None, "himalayas_missing_valid_guid", {}
    p = urlsplit(guid)
    if not re.fullmatch(r"/companies/[^/]+/jobs/[^/]+/?", p.path):
        return None, "himalayas_missing_api_job_path", {}
    api_url = "https://himalayas.app/jobs/api" + p.path.rstrip("/")
    req = Request(api_url, headers={"Accept": "application/json",
                                    "User-Agent": "AI-Job-Search-Agent/1.0"})
    try:
        with urlopen(req, timeout=timeout) as response:
            data = json.load(response)
            if not isinstance(data, dict) or not isinstance(data.get("job"), dict):
                return None, "himalayas_invalid_detail_response", {}
            detail = data["job"]
            if (str(detail.get("guid") or "").rstrip("/") and
                urlsplit(str(detail["guid"])).path.rstrip("/") != p.path.rstrip("/")):
                return None, "himalayas_guid_mismatch", {}
            return True, "himalayas_api_open", detail
    except HTTPError as exc:
        if exc.code in (404, 410):
            return False, f"himalayas_api_http_{exc.code}", {}
        return None, f"himalayas_api_http_{exc.code}", {}
    except (ValueError, TypeError, URLError, TimeoutError, OSError):
        return None, "himalayas_api_unavailable", {}


def board_date(row: dict, now: datetime) -> tuple[datetime | None, str]:
    from app.freshness import _parse_posting_value
    source = (row.get("source") or "").lower()
    # Himalayas pubDate is **last updated** according to API docs, not published.
    if source == "himalayas":
        values = ("posted_on", "posted_at", "pubDate", "updated_at", "freshness_verified_posted_at")
        basis = "job_board_last_updated"
    else:
        values = ("posted_at", "posted_on", "date_posted", "datePosted",
                  "published_at", "publication_date", "freshness_verified_posted_at")
        basis = "job_board_posting_date"
    for field in values:
        value = row.get(field)
        if value in (None, ""):
            continue
        parsed = _parse_posting_value(str(value), now)
        if parsed:
            return parsed, basis
    return None, "job_board_date_unverified"
