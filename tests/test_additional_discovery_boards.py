"""New job-discovery board regression tests. Network is mocked, never scraped in CI."""
import json
from pathlib import Path

import pytest

from app.sources import hiringcafe, himalayas
from app import discovery, application_queue


def test_hiringcafe_normalize_is_discovery_only():
    got = hiringcafe.normalize({
        "id": "42", "title": "Senior Data Engineer",
        "company": {"name": "Example Inc"}, "location": "New York, NY",
        "url": "https://hiringcafe.com/job/42",
        "posted_at": "2026-10-29T10:00:00Z",
        "employment_type": "Full Time",
    })
    assert got["source"] == "hiringcafe"
    assert got["company_key"] == "Example Inc"
    assert got["posted_on"] == "2026-10-29T10:00:00+00:00"
    assert got["discovery_only"] is True
    assert got["authoritative_source"] is False
    assert hiringcafe.normalize({"title": "Nurse", "url": "https://hiringcafe.com/job/43"}) is None


def test_hiringcafe_uses_official_bounded_mcp(monkeypatch):
    calls = []
    monkeypatch.setattr(hiringcafe, "call_tool", lambda endpoint, tool, args, retries=1:
        calls.append((endpoint, tool, args, retries)) or
        {"jobs": [{"title": "Data Engineer", "company": "Example",
                   "url": "https://hiringcafe.com/job/example"}]})
    jobs = hiringcafe.fetch_jobs()
    assert len(jobs) == 1
    assert len(calls) == 1
    assert calls[0][0] == "https://hiringcafe.com/api/mcp"
    assert calls[0][1] == "search_jobs"
    assert calls[0][2]["limit"] <= 25


def test_himalayas_location_restrictions_application_link_and_dates():
    item = {
        "guid": "https://himalayas.app/companies/acme/jobs/data-engineer",
        "applicationLink": "https://himalayas.app/companies/acme/jobs/data-engineer",
        "title": "Data Engineer", "companyName": "Acme",
        "locationRestrictions": ["US"], "employmentType": "Full Time",
        "pubDate": 1793268000, "description": "<p>Data warehouse and pipelines</p>"
    }
    got = himalayas.normalize(item)
    assert got["source"] == "himalayas"
    assert got["discovery_only"] is True
    assert got["source_credit"] == "Himalayas"
    assert got["source_apply_url"] == item["applicationLink"]
    assert got["posted_on"].startswith("2026")
    assert got["location"] == "Remote, United States"
    assert himalayas.normalize({**item, "locationRestrictions": ["GB"]}) is None
    assert himalayas.normalize({**item, "applicationLink": "https://evil.example/phishing", "guid": ""}) is None


def test_himalayas_search_is_country_scoped_and_paginated(monkeypatch):
    requested = []
    def fake_get(url):
        requested.append(url)
        return {"jobs": [{
            "title": "Data Engineer", "companyName": "Acme",
            "applicationLink": "https://himalayas.app/companies/acme/jobs/data-engineer",
            "guid": "https://himalayas.app/companies/acme/jobs/data-engineer",
            "locationRestrictions": ["US"], "pubDate": 1793268000
        }]}
    monkeypatch.setattr(himalayas, "_get", fake_get)
    rows = himalayas.fetch_jobs(max_pages=1)
    assert len(rows) == 1
    assert len(requested) == len(himalayas.SEARCHES)
    assert all("country=US" in url for url in requested)


def test_discovery_wires_both_sources_without_touching_other_paths(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(discovery, "hiringcafe_jobs", lambda **kw: calls.append(("hiringcafe", kw)) or [])
    monkeypatch.setattr(discovery, "himalayas_jobs", lambda **kw: calls.append(("himalayas", kw)) or [])
    monkeypatch.setattr(discovery, "resolve_original_ats", lambda row: row)
    monkeypatch.setattr(discovery, "learn_from_jobs", lambda rows, registry: [])
    monkeypatch.setattr(discovery, "load_registry", lambda path: {})
    config = {"hiringcafe": {"enabled": True},
              "himalayas": {"enabled": True, "max_pages": 1}}
    rows, errors, coverage = discovery.discover(
        config, registry_path=str(tmp_path / "registry.json"),
        health_path=str(tmp_path / "health.json"), return_coverage=True)
    assert rows == [] and not errors
    assert {call[0] for call in calls} == {"hiringcafe", "himalayas"}
    assert coverage["configured_units_by_provider"]["hiringcafe"] == 1
    assert coverage["configured_units_by_provider"]["himalayas"] == 1
    assert set(coverage["job_board_or_discovery_providers_attempted"]) >= {"hiringcafe", "himalayas"}


def test_config_retains_existing_yc_and_adds_wttj_and_two_api_adapters():
    cfg = json.loads(Path("data/job_sources.json").read_text())
    assert cfg["hiringcafe"]["enabled"] is True
    assert cfg["himalayas"]["enabled"] is True
    portals = cfg["discovery_portal"]
    assert any(x.get("provider") == "yc_jobs" and x.get("enabled") for x in portals)
    wttj = next(x for x in portals if x["provider"] == "welcome_to_the_jungle")
    assert wttj["enabled"] is True
    assert "jobs-data-us" in wttj["search_url"]


def test_queue_preserves_himalayas_apply_link(monkeypatch, tmp_path):
    doc = tmp_path / "resume.pdf"
    doc.write_bytes(b"mock PDF")
    r = {
        "external_id": "himalayas:123", "source": "himalayas",
        "company": "Acme", "title": "Data Engineer", "location": "Remote, United States",
        "employment_type": "Full Time", "description": "data engineer",
        "next_action": "READY_TO_APPLY", "pdf_path": str(doc),
        "original_url": "https://jobs.ashbyhq.com/acme/abc",
        "url": "https://himalayas.app/companies/acme/jobs/data-engineer",
        "source_apply_url": "https://himalayas.app/companies/acme/jobs/data-engineer",
        "source_credit": "Himalayas",
        "ats_provider": "ashby",
        "artifact_validation": {"passed": True}
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([r]))
    monkeypatch.setattr(application_queue, "load_profile", lambda: {})
    monkeypatch.setattr(application_queue, "passes_hard_filters", lambda *args: (True, []))
    items = application_queue.build(str(manifest), str(tmp_path / "queue.json"))
    assert len(items) == 1
    assert items[0]["url"] == r["source_apply_url"]
    assert items[0]["source_credit"] == "Himalayas"


def test_namespaced_mcp_cards_are_read_and_text_posting_dates_preserved(monkeypatch):
    from types import SimpleNamespace
    from app.sources.mcp_jobs import _content_to_payload
    raw = SimpleNamespace(
        structured_content=None,
        meta={"hiringcafe.com/job-cards": {"jobs": [{
            "id": "cafe-abc", "title": "Data Engineer", "company": "Example",
            "url": "https://hiringcafe.com/job/example", "meta": ["Austin · Remote", "Full Time · Senior"]
        }]}},
        content=[SimpleNamespace(text=(
            "## HiringCafe job results\n\n"
            "### [Data Engineer](https://hiringcafe.com/job/example)\n"
            "- **Posted:** 2026-10-29\n"
            "- **Job ID:** cafe-abc\n"
        ))]
    )
    payload = _content_to_payload(raw)
    assert len(payload["jobs"]) == 1
    assert "2026-10-29" in payload["_mcp_text"]
    assert hiringcafe._markdown_posting_details(payload["_mcp_text"])["cafe-abc"]["posted"] == "2026-10-29"
    monkeypatch.setattr(hiringcafe, "call_tool", lambda *a, **kw: payload)
    jobs = hiringcafe.fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["posted_on"] == "2026-10-29T00:00:00+00:00"
    assert jobs[0]["employment_type"] == "Full Time"
    assert "Austin" in jobs[0]["location"]
