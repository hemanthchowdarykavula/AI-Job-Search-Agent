"""Himalayas public Remote Jobs API discovery adapter (no scraping).

API terms: preserve the supplied applicationLink for Apply actions and credit
Himalayas in dashboard presentation. Official employer/ATS JD + posted date
must still be verified by the existing production finalization gates.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ENDPOINT = "https://himalayas.app/jobs/api/search"
SEARCHES = ("data engineer", "analytics engineer", "data platform engineer")
MAX_PAGES = 2
SOURCE_URL = "https://himalayas.app"
_TIMEOUT = 20


def _text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def _posted(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{10}(?:\.\d+)?", str(value)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError, TypeError):
        return None


def _url(value):
    out = str(value or "").strip()
    if not out.startswith("https://"):
        return ""
    p = urlparse(out)
    if not p.hostname or not (p.hostname == "himalayas.app" or p.hostname.endswith(".himalayas.app")):
        # Only accept source-owned link for display/Apply attribution.
        return ""
    return out


def normalize(job):
    title = _text(job.get("title"))
    if not title or not any(x in title.lower() for x in ("data engineer", "data platform", "analytics engineer", "data infrastructure", "etl engineer")):
        return None
    url = _url(job.get("applicationLink") or job.get("guid"))
    if not url:
        return None
    company = _text(job.get("companyName")) or "Unknown"
    restrictions = job.get("locationRestrictions") or []
    if not isinstance(restrictions, list):
        return None
    countries = [str(x).upper() for x in restrictions]
    # A posting restricted to e.g. the UK must not masquerade as US-eligible.
    if countries and not any(x in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"} for x in countries):
        return None
    posted = _posted(job.get("pubDate"))
    guid = str(job.get("guid") or url)
    stable = hashlib.sha1(guid.encode()).hexdigest()[:20]
    description = _text(job.get("description") or job.get("excerpt"))
    return {
        "external_id": f"himalayas:{stable}", "source": "himalayas",
        "company_key": company, "title": title,
        "location": "Remote, United States" if countries else "Remote (Worldwide; US eligible)",
        "employment_type": job.get("employmentType"),
        "url": url, "original_url": url,
        "applicationLink": url, "source_credit": "Himalayas",
        "source_url": SOURCE_URL, "description": description,
        "description_complete": False, "discovery_only": True,
        "authoritative_source": False, "portal_source": "himalayas",
        "posted_on": posted, "updated_at": posted,
    }


def _get(url, *, timeout=_TIMEOUT):
    req = Request(url, headers={"User-Agent": "AI-Job-Search-Agent/1.0 (personal job research)",
                                "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def fetch_jobs(hours: float = 24, max_pages: int = MAX_PAGES) -> list[dict]:
    """Targeted US search; no page scraping or large all-jobs backfill."""
    out = {}
    succeeded = 0
    errors = []
    for query in SEARCHES:
        for page in range(1, max(1, min(3, max_pages)) + 1):
            url = ENDPOINT + "?" + urlencode({"q": query, "country": "US", "sort": "recent", "page": page})
            try:
                payload = _get(url)
                if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                    raise ValueError("Unexpected Himalayas API response")
                succeeded += 1
                rows = payload["jobs"]
                for job in rows:
                    if isinstance(job, dict):
                        item = normalize(job)
                        if item:
                            out[item["external_id"]] = item
                if not rows:
                    break
            except HTTPError as exc:
                # 429 means stop, don't retry/circumvent shared public rate limits.
                errors.append(f"{query} page {page}: HTTP {exc.code}")
                if exc.code == 429:
                    if succeeded == 0:
                        raise RuntimeError("Himalayas public API rate limit (429)") from exc
                    return list(out.values())
                break
            except (OSError, ValueError, TimeoutError) as exc:
                errors.append(f"{query} page {page}: {type(exc).__name__}")
                break
    if not succeeded and errors:
        raise RuntimeError("Himalayas public API unavailable: " + "; ".join(errors[:2]))
    if errors:
        print(f"Himalayas partial API results: {len(out)} jobs; {len(errors)} failed pages", flush=True)
    return list(out.values())
