from __future__ import annotations
import html,json,re
from http.client import InvalidURL
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib import request,parse
from app.config import load_profile
from app.eligibility import two_category_filter
from app.filters import passes_hard_filters
from app.ats_resolver import resolve_original_ats
from app.sources.workday import job_detail_is_live
from app.discovery import ALL_ATS_PROVIDERS
from app.company_domain_resolver import resolve_company
from app.employer_job_resolver import resolve as resolve_employer_job
from app.provider_adapter_router import fetch_exact_job
from app.source_registry import detect_ats
from app.company_registry import company_key as registry_company_key, load as load_company_registry, save as save_company_registry
from app.approved_job_boards import (
    approved_board, board_url_is_valid, board_application_url, board_date,
    himalayas_open_details, BOARD_APPLICATION_ROUTE,
)
from urllib.error import HTTPError, URLError

MIN_COMPLETE_JD_CHARS=1200
MIN_JD_SIGNAL_SCORE=3
MIN_USABLE_JD_CHARS=250
DICE_BOILERPLATE_MARKERS=("Search all similar jobs","Jobs Directory","Career Advice","Employers and Recruiters","Get the Dice app","Copyright ©","Apply Now To see how well you match")
AGGREGATOR_HOSTS=("dice.com","indeed.com","linkedin.com","ziprecruiter.com","monster.com","adzuna.com","glassdoor.com","simplyhired.com","careerbuilder.com","hiringcafe.com","himalayas.app","welcometothejungle.com")
JOB_BOARD_SOURCES={"dice","ziprecruiter","indeed","linkedin","monster","adzuna","glassdoor","simplyhired","careerbuilder","hiringcafe","himalayas","welcome_to_the_jungle","yc_jobs","wellfound","builtin"}

def _is_aggregator_url(url):
    host=(parse.urlsplit(url or "").netloc or "").lower()
    return any(host==name or host.endswith("."+name) for name in AGGREGATOR_HOSTS)
JD_SECTION_SIGNALS=("responsibilities","requirements","qualifications","what you'll do","what you will do","skills","experience","preferred","minimum qualifications","basic qualifications")

def _jd_signal_score(text):
    low=(text or "").lower()
    return sum(1 for s in JD_SECTION_SIGNALS if s in low)

def _looks_like_complete_jd(text,source=""):
    value=(text or "").strip()
    if len(value)<MIN_COMPLETE_JD_CHARS:return False
    if (source or "").lower()=="dice" and sum(m.lower() in value.lower() for m in DICE_BOILERPLATE_MARKERS)>=2:return False
    return _jd_signal_score(value)>=MIN_JD_SIGNAL_SCORE

def _looks_like_usable_jd(text,source=""):
    value=(text or "").strip()
    if len(value)<MIN_USABLE_JD_CHARS:return False
    if (source or "").lower()=="dice" and sum(m.lower() in value.lower() for m in DICE_BOILERPLATE_MARKERS)>=2:return False
    # Short provider excerpts are allowed for conservative base-resume tailoring
    # when they contain at least one meaningful JD section signal.
    return _jd_signal_score(value)>=1

def _clean_html(text):
    text=re.sub(r"(?is)<(script|style).*?>.*?</\1>"," ",text or "")
    text=re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h[1-6]>","\n",text)
    text=re.sub(r"(?s)<[^>]+>"," ",text)
    text=html.unescape(text).replace("\xa0"," ")
    return re.sub(r"[ \t]+"," ",re.sub(r"\n\s*\n+","\n",text)).strip()

DEAD_PAGE_MARKERS=("job is no longer available","job no longer available","position is no longer available","position has been filled","job has been filled","job has expired","posting has expired","requisition has been closed","this job is closed","page not found","job not found","no longer accepting applications")

def _live_public_job_page(url):
    """Provider-agnostic final existence check before paid resume generation."""
    if not url:return False,"missing_url"
    req=request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; AI-Job-Search-Agent/1.0)","Accept":"text/html,application/json,*/*"})
    try:
        with request.urlopen(req,timeout=30) as resp:
            status=getattr(resp,"status",200)
            if status in (404,410):return False,f"http_{status}"
            if status>=400:return False,f"http_{status}"
            body=resp.read(500000).decode("utf-8",errors="replace")
    except HTTPError as exc:
        if exc.code in (404,410):
            return False,f"http_{exc.code}"
        # Access blocks, rate limits, and transient server errors do not prove
        # that the requisition is dead. Hold until it can be verified.
        return None,f"http_{exc.code}"
    except (URLError,TimeoutError,OSError,ValueError,InvalidURL):
        # Network/anti-bot failures and malformed URLs are not allowed to crash
        # the whole production cycle. Hold this one job for later verification.
        return None,"unverifiable"
    plain=_clean_html(body).lower()
    if any(marker in plain for marker in DEAD_PAGE_MARKERS):
        return False,"closed_marker"
    return True,"reachable"

def _fetch_public_page(url):
    if not url:return ""
    req=request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; AI-Job-Search-Agent/1.0)"})
    try:
        with request.urlopen(req,timeout=30) as resp:return resp.read().decode("utf-8",errors="replace")
    except Exception:return ""

def _jsonld_jobpostings(page):
    """Return schema.org JobPosting nodes for identity-aware employer resolution."""
    found=[]
    for block in re.findall(r"(?is)<script[^>]+type=['\"]application/ld\+json['\"][^>]*>(.*?)</script>",page or ""):
        try: payload=json.loads(html.unescape(block).strip())
        except Exception: continue
        stack=payload if isinstance(payload,list) else [payload]
        for item in stack:
            if not isinstance(item,dict):continue
            nodes=item.get("@graph") if isinstance(item.get("@graph"),list) else [item]
            for node in nodes:
                if not isinstance(node,dict):continue
                kinds=node.get("@type");kinds=kinds if isinstance(kinds,list) else [kinds]
                if "JobPosting" in kinds:found.append(node)
    return found

def _extract_jsonld_job_description(page):
    for node in _jsonld_jobpostings(page):
        if node.get("description"):return _clean_html(str(node["description"]))
    return ""

def _official_posted_at(page,now=None):
    """Extract the employer/ATS JobPosting publication date when the page exposes one."""
    now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for node in _jsonld_jobpostings(page):
        value=node.get("datePosted") or node.get("datePublished")
        if not value:continue
        try:return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc),str(value)
        except Exception:continue
    plain=_clean_html(page)
    m=re.search(r"(?i)\bposted\s+(today|just now|(?:an?|\d+)\s+hours?\s+ago|(?:a|\d+)\s+days?\s+ago)\b",plain)
    if not m:return None,None
    label="Posted "+m.group(1)
    from app.freshness import _parse_posting_value
    return _parse_posting_value(label,now),label

def _norm_identity(value):
    return re.sub(r"[^a-z0-9]+"," ",(value or "").lower()).strip()

def _jobposting_identity_matches(job,node,url):
    """Require a candidate page to identify the same employer and role, not merely mention them."""
    expected_title=_norm_identity(job.get("title"))
    actual_title=_norm_identity(str(node.get("title") or ""))
    if not expected_title or not actual_title:return False
    et=set(expected_title.split());at=set(actual_title.split())
    title_ratio=len(et & at)/max(1,len(et))
    if title_ratio<0.70:return False
    org=node.get("hiringOrganization") or {}
    org_name=_norm_identity(org.get("name") if isinstance(org,dict) else str(org))
    company=_norm_identity(job.get("company_key") or job.get("company"))
    company_tokens={x for x in company.split() if len(x)>=4 and x not in {"company","corporation","inc","llc","ltd"}}
    org_tokens=set(org_name.split())
    host=_norm_identity(parse.urlsplit(url).netloc).replace(" ","")
    company_ok=bool(company_tokens & org_tokens) if org_name else any(x in host for x in company_tokens)
    if not company_ok:return False
    expected_id=_norm_identity(str(job.get("requisition_id") or job.get("job_id") or job.get("external_id") or ""))
    ident=node.get("identifier") or {}
    actual_id=_norm_identity(str(ident.get("value") if isinstance(ident,dict) else ident))
    # When both sides expose a useful requisition identifier, disagreement is a hard mismatch.
    if expected_id and actual_id and len(expected_id)>=4 and len(actual_id)>=4 and expected_id not in actual_id and actual_id not in expected_id:
        # Aggregator external IDs are often unrelated UUIDs; only enforce recognizable requisition-like IDs.
        if any(ch.isdigit() for ch in expected_id) and len(expected_id)<80:return False
    return True

def _mark_employer_resolution_pending(job):
    company=(job.get("company_key") or job.get("company") or "").strip()
    if not company:return
    try:
        reg=load_company_registry()
        row=reg.setdefault(registry_company_key(company),{"company":company})
        row["company"]=company
        row["current_hiring_signal"]=True
        row["ats_resolution_pending"]=True
        row["ats_resolution_pending_at"]=datetime.now(timezone.utc).isoformat()
        row["ats_resolution_pending_title"]=job.get("title")
        row["ats_resolution_pending_source"]=job.get("source")
        row["ats_resolution_pending_url"]=job.get("aggregator_url") or job.get("url")
        save_company_registry(reg)
    except Exception:
        pass

def _jobposting_metadata(page):
    nodes=_jsonld_jobpostings(page)
    if not nodes:return {}
    node=max(nodes,key=lambda n:len(str(n.get("description") or "")))
    org=node.get("hiringOrganization")
    company=(org.get("name") if isinstance(org,dict) else "") or ""
    identifier=node.get("identifier")
    if isinstance(identifier,dict):
        identifier=identifier.get("value") or identifier.get("name")
    locations=[]
    locs=node.get("jobLocation")
    locs=locs if isinstance(locs,list) else [locs] if isinstance(locs,dict) else []
    if str(node.get("jobLocationType") or "").upper()=="TELECOMMUTE":locations.append("Remote")
    for loc in locs:
        address=loc.get("address") if isinstance(loc,dict) else None
        if not isinstance(address,dict):continue
        text=", ".join(str(address.get(k)).strip() for k in ("addressLocality","addressRegion","addressCountry") if address.get(k))
        if text and text not in locations:locations.append(text)
    return {
        "title":str(node.get("title") or node.get("name") or "").strip(),
        "company":str(company).strip(),
        "description":_clean_html(str(node.get("description") or "")),
        "location":" / ".join(locations),
        "employment_type":node.get("employmentType"),
        "posted_at":node.get("datePosted"),
        "requisition_id":str(identifier or "").strip() or None,
        "jsonld_url":str(node.get("url") or "").strip() or None,
        "metadata_resolution_source":"jobposting_jsonld",
    }


def _workday_authoritative_metadata(url,provider=None,identifier=None):
    provider=provider or detect_ats(url)[0]
    identifier=identifier or detect_ats(url)[1]
    if provider!="workday" or not identifier or "|" not in str(identifier):return {}
    tenant,site=str(identifier).split("|",1)
    parsed=parse.urlsplit(url or "")
    host=parsed.hostname or ""
    path=parsed.path or ""
    marker=f"/{site}/"
    idx=path.lower().find(marker.lower())
    if idx>=0:
        external_path="/"+path[idx+len(marker):].lstrip("/")
    else:
        job_idx=path.lower().find("/job/")
        external_path=path[job_idx:] if job_idx>=0 else ""
    if not host or not tenant or not site or not external_path:return {}
    live,detail=job_detail_is_live(host,tenant,site,external_path)
    if not live or not isinstance(detail,dict):return {}
    description=_clean_html(str(detail.get("jobDescription") or ""))
    locations=[str(detail.get("location") or "").strip()]
    for item in detail.get("additionalLocations") or []:
        if item:locations.append(str(item).strip())
    locations=[x for x in locations if x]
    return {
        "title":str(detail.get("title") or "").strip(),
        "description":description,
        "location":" | ".join(dict.fromkeys(locations)),
        "employment_type":detail.get("timeType") or detail.get("workerType"),
        "posted_at":detail.get("postedOn") or detail.get("postedDate"),
        "requisition_id":str(detail.get("jobReqId") or detail.get("jobPostingId") or "").strip() or None,
        "job_id":str(detail.get("jobPostingId") or detail.get("jobReqId") or "").strip() or None,
        "metadata_resolution_source":"workday_cxs",
    }


def enrich_authoritative_job_metadata(job):
    """Populate canonical title/company/JD metadata using the same provider/page rules for every path."""
    out=dict(job or {})
    url=out.get("original_url") or out.get("url") or ""
    provider,identifier=detect_ats(url)
    provider=provider or out.get("ats_provider")
    identifier=identifier or out.get("ats_identifier")
    if provider:out["ats_provider"]=provider
    if identifier:out["ats_identifier"]=identifier

    # Exact provider detail is the strongest source for a supplied/direct ATS URL.
    # It must outrank a longer client-rendered HTML shell: authoritative quality
    # is determined by source provenance, not by character count.
    exact_meta={}
    if provider and url and not _is_aggregator_url(url):
        company=str(out.get("company_key") or out.get("company") or "").strip()
        try:
            exact,exact_provider,exact_identifier=fetch_exact_job(
                provider,
                company,
                {
                    "original_url":url,
                    "url":url,
                    "ats_provider":provider,
                    "ats_identifier":identifier,
                },
            )
        except Exception:
            exact=exact_provider=exact_identifier=None
        if isinstance(exact,dict):
            exact_meta=dict(exact)
            if exact_provider:
                out["ats_provider"]=exact_provider
                provider=exact_provider
            if exact_identifier:
                out["ats_identifier"]=exact_identifier
                identifier=exact_identifier
            exact_meta["metadata_resolution_source"]=exact_meta.get("exact_job_metadata_source") or "exact_ats_detail"

    page=_fetch_public_page(url) if url else ""
    jsonld=_jobposting_metadata(page) if page else {}
    provider_meta=_workday_authoritative_metadata(url,provider,identifier)
    authoritative=exact_meta or provider_meta or jsonld

    # Exact provider detail APIs outrank provider/page metadata. Never replace an
    # exact API JD merely because the HTML page shell is longer.
    if authoritative:
        if authoritative.get("title"):out["title"]=authoritative["title"]
        company=str(authoritative.get("company_key") or authoritative.get("company") or "").strip()
        if company:
            out["company_key"]=company
            out["company"]=company
        for key in ("location","employment_type","requisition_id","job_id","posted_at"):
            if authoritative.get(key) not in (None,"",[],{}):out[key]=authoritative.get(key)
        desc=str(authoritative.get("description") or "").strip()
        if desc and _looks_like_usable_jd(desc,provider or out.get("source") or ""):
            out["description"]=desc
            out["description_length"]=len(desc)
            out["description_source"]=authoritative.get("metadata_resolution_source") or "authoritative_provider_detail"
        out["metadata_resolution_source"]=authoritative.get("metadata_resolution_source")
        out["metadata_verified"]=True
    return out


def _best_resolved_description(page,source=""):
    jsonld=_extract_jsonld_job_description(page)
    extracted=_extract_dice(page) if (source or "").lower()=="dice" else _clean_html(page)
    candidates=[x.strip() for x in (jsonld,extracted) if x and x.strip()]
    if not candidates:return ""
    # Prefer meaningful JD structure first, then length. This avoids replacing a
    # clean JSON-LD JobPosting with a much longer navigation-heavy HTML dump.
    return max(candidates,key=lambda x:(_jd_signal_score(x),len(x)))

def _extract_dice(page):
    plain=_clean_html(page)
    start=re.search(r"(?i)\bJob Description\b",plain)
    if start:plain=plain[start.start():]
    end=re.search(r"(?i)\b(?:Search all similar jobs|Similar Jobs|More jobs at|Search for Jobs|Jobs Directory|Career Advice|Employers and Recruiters|Create a job alert|Dice Id:)\b",plain)
    if end:plain=plain[:end.start()]
    return plain

def _resolve_employer_career_page(job):
    """Resolve an aggregator lead only to a strongly identity-matched employer/ATS JobPosting."""
    company=(job.get("company_key") or job.get("company") or "").strip()
    title=(job.get("title") or "").strip()
    if not company or not title:return ("","")
    # Build several identity-rich lookup routes instead of relying on only
    # company+title. Aggregator leads often expose a requisition ID, location, or
    # hiringOrganization URL that is more discriminating than the title alone.
    req_id=str(job.get("requisition_id") or job.get("job_id") or "").strip()
    location=str(job.get("location") or "").strip()
    domain_hit=resolve_company({
        "company":company,
        "organization_url_evidence":job.get("organization_url_evidence"),
        "official_domain":job.get("official_domain"),
        "official_url":job.get("official_url"),
    },allow_name_search=True) or {}
    official_domain=(domain_hit.get("official_domain") or "").strip()
    queries=[f'"{title}" "{company}" careers',f'"{title}" "{company}" jobs']
    if req_id:queries.insert(0,f'"{req_id}" "{company}" jobs')
    if location:queries.append(f'"{title}" "{company}" "{location}"')
    if official_domain:
        queries.insert(0,f'site:{official_domain} "{title}"')
        if req_id:queries.insert(0,f'site:{official_domain} "{req_id}"')
    seen=set();ranked=[]
    for query in queries:
        page=_fetch_public_page("https://www.google.com/search?q="+parse.quote(query))
        urls=re.findall(r'https?://[^&"<> ]+',page or "")
        for u in urls:
            u=html.unescape(u)
            if u in seen:continue
            seen.add(u)
            low=u.lower()
            if any(x in low for x in ("dice.com","indeed.com","linkedin.com","ziprecruiter.com","google.com")):continue
            p=_fetch_public_page(u)
            for node in _jsonld_jobpostings(p):
                if not _jobposting_identity_matches(job,node,u):continue
                desc=_clean_html(str(node.get("description") or ""))
                if not _looks_like_usable_jd(desc,""):continue
                provider_bonus=1 if any(x in low for x in ("greenhouse","lever.co","ashbyhq","myworkdayjobs","smartrecruiters","icims","jobvite","oraclecloud")) else 0
                ranked.append((provider_bonus,_jd_signal_score(desc),len(desc),u,desc))
    if not ranked:return ("","")
    _,_,_,u,desc=max(ranked)
    return u,desc

_GENERIC_RESOLVED_TITLES={"","job","job opening","job details","job detail","jobs","careers","career opportunities","job from supplied link"}


def _is_generic_resolved_title(value):
    return re.sub(r"\s+"," ",str(value or "")).strip().lower() in _GENERIC_RESOLVED_TITLES


def _recover_title_from_jd_text(text):
    """Recover an explicit role title from JD prose when client-rendered metadata is missing."""
    value=re.sub(r"\s+"," ",str(text or "")).strip()
    if not value:return ""
    patterns=(
        r"(?i)\bcurrently\s+seeking\s+(?:an?\s+)?([^.;]{3,160})",
        r"(?i)\bis\s+seeking\s+(?:an?\s+)?([^.;]{3,160})",
        r"(?i)\bseeking\s+(?:an?\s+)?([^.;]{3,160})",
        r"(?i)\bposition\s+title\s*[:\-]\s*([^.;]{3,160})",
        r"(?i)\bjob\s+title\s*[:\-]\s*([^.;]{3,160})",
        r"(?i)\bas\s+(?:an?\s+)?([^,.;]{3,140}),\s+you\b",
    )
    role_hint=re.compile(r"(?i)\b(ai|ml|data|analytics|engineer|engineering|developer|architect|scientist|platform|software|manager|lead|principal|staff)\b")
    for pattern in patterns:
        match=re.search(pattern,value)
        if not match:continue
        candidate=re.sub(r"(?i)\s+(?:to join|who will|that will|responsible for)\b.*$","",match.group(1)).strip(" |–—-,:")
        candidate=re.sub(r"\s+"," ",candidate)
        if 3<=len(candidate)<=180 and role_hint.search(candidate):
            return candidate
    return ""


def _merge_authoritative_match(base,matched):
    out=dict(base)
    if not isinstance(matched,dict):return out
    for key in (
        "title","location","employment_type","requisition_id","job_id",
        "posted_at","posted_on","date_posted","updated_at",
        "ats_provider","ats_identifier","original_url","url",
    ):
        if matched.get(key) not in (None,"",[],{}):out[key]=matched.get(key)
    desc=str(matched.get("description") or "").strip()
    if desc:
        out["description"]=desc
        out["description_length"]=len(desc)
    out["ats_resolution"]=matched.get("ats_resolution") or "verified_employer_source_match"
    out["employer_resolution_match_score"]=matched.get("resolver_match_score")
    out["metadata_resolution_source"]="verified_employer_source_match"
    out["metadata_verified"]=True
    return out


def resolve_full_jd(job):
    """Resolve the JD and preserve user-approved source-hosted apply routes."""
    if approved_board(job.get("source")):
        out = dict(job)
        source = out.get("source") or ""
        board_url = board_application_url(out)
        if board_url:
            out["board_url"] = board_url
            out["source_apply_url"] = board_url
            out["original_url"] = board_url
            out["application_route"] = BOARD_APPLICATION_ROUTE
        current = (out.get("description") or "").strip()
        # If the job board supplies a full description, do not needlessly
        # search an unrelated employer ATS or overwrite the board apply link.
        if not _looks_like_usable_jd(current, source) and board_url and source != "himalayas":
            page = _fetch_public_page(board_url)
            resolved = _best_resolved_description(page, source)
            if _looks_like_usable_jd(resolved, source) and len(resolved) > len(current):
                out["description"] = resolved
        desc = (out.get("description") or "").strip()
        out["description_length"] = len(desc)
        out["description_complete"] = _looks_like_complete_jd(desc, source)
        out["description_usable"] = _looks_like_usable_jd(desc, source)
        out["jd_signal_score"] = _jd_signal_score(desc)
        out["jd_resolution_source"] = "approved_job_board_listing"
        out["posting_verification_scope"] = "JOB_BOARD_ONLY"
        out["source_credit"] = out.get("source_credit") or source.replace("_", " ").title()
        return out
    job=resolve_original_ats(job)
    job=enrich_authoritative_job_metadata(job)
    current=(job.get("description") or "").strip()

    # Client-rendered ATS pages can expose the complete JD while omitting title
    # metadata from the server HTML. Recover only explicit title wording from the
    # JD, then use the same verified employer/source registry used by production
    # discovery to canonicalize the full job record.
    recovered_title=""
    if _is_generic_resolved_title(job.get("title")):
        recovered_title=_recover_title_from_jd_text(current)
        if recovered_title:
            job["title"]=recovered_title
            job["title_resolution_source"]="jd_explicit_phrase"

    noisy_payload=len(current)>50000
    needs_canonical=(
        not job.get("metadata_verified")
        and bool((job.get("company_key") or job.get("company")) and job.get("title"))
        and (bool(recovered_title) or noisy_payload)
    )
    if needs_canonical:
        matched=resolve_employer_job(job)
        if matched:
            job=_merge_authoritative_match(job,matched)
            current=(job.get("description") or "").strip()
    source=(job.get("source") or "").lower()
    lead_before_resolution=job.get("original_url") or job.get("url") or ""
    aggregator_origin=bool(job.get("discovery_only")) or source in JOB_BOARD_SOURCES or _is_aggregator_url(lead_before_resolution)

    # A provider-native exact-detail resolver is stronger than a longer HTML
    # shell. Recompute completeness after authoritative metadata enrichment so
    # a clean API JD is never replaced merely because the public page contains
    # more navigation/legal text.
    # Keep an exact provider description as the source of truth even when it
    # is legitimately brief. It can still fail completeness gating, but should
    # not be overwritten by navigation/footer content from a longer page shell.
    if job.get("metadata_verified") and _looks_like_usable_jd(current,source) and not aggregator_origin:
        job["description_usable"]=True
        job["description_complete"]=_looks_like_complete_jd(current,source)
        job["description_length"]=len(current)
        job["jd_signal_score"]=_jd_signal_score(current)
        job["jd_resolution_source"]="authoritative_provider_detail"
        return job
    if job.get("metadata_verified") and _looks_like_complete_jd(current,source) and not aggregator_origin:
        job["description_complete"]=True
        job["description_usable"]=True
        job["description_length"]=len(current)
        job["jd_signal_score"]=_jd_signal_score(current)
        job["jd_resolution_source"]="authoritative_provider_detail"
        return job
    if job.get("description_complete") and _looks_like_complete_jd(current,source) and not aggregator_origin:return job
    fetch_url=job.get("original_url") or job.get("url")
    page=_fetch_public_page(fetch_url)
    resolved=_best_resolved_description(page,source)
    out=dict(job)
    employer_url=""
    # Aggregators can expose only a teaser and omit the employer ATS link. In
    # that case, resolve the same company/title on the employer's public career
    # site rather than weakening JD quality requirements.
    # Aggregator URLs are discovery leads, not preferred application targets.
    # Always try to canonicalize discovery-board jobs to the employer's own
    # careers/ATS page, even when the board already supplied a usable/full JD.
    lead_url=out.get("original_url") or out.get("url") or ""
    # Any aggregator-origin lead is discovery-only. It must resolve to the
    # employer/ATS job page before it can become eligible for paid resume work.
    should_resolve_employer=(bool(out.get("discovery_only")) or source in JOB_BOARD_SOURCES or _is_aggregator_url(lead_url))
    if should_resolve_employer or not _looks_like_usable_jd(resolved or current,source):
        # First use verified company/source registries and provider APIs to find
        # the exact employer job. This is more reliable than web-search HTML and
        # reuses sources already proven by production discovery/enrichment.
        matched=resolve_employer_job(out) if should_resolve_employer else None
        if matched:
            employer_url=matched.get("original_url") or matched.get("url") or ""
            employer_desc=(matched.get("description") or "").strip()
            out["aggregator_url"]=out.get("aggregator_url") or out.get("original_url") or out.get("url")
            out["job_board_posted_at"]=next((out.get(field) for field in ("posted_at","posted_on","date_posted","datePosted","published_at","publication_date","updated_at") if out.get(field) not in (None,"")),None)
            # Copy authoritative employer fields without replacing the discovery
            # source identity; freshness later still prefers the employer page.
            for key in ("location","employment_type","requisition_id","job_id","posted_at","posted_on","date_posted","updated_at","ats_provider","ats_identifier"):
                if matched.get(key) not in (None,"",[],{}):out[key]=matched.get(key)
            out["original_url"]=employer_url
            out["ats_provider"]=matched.get("ats_provider") or out.get("ats_provider")
            out["ats_identifier"]=matched.get("ats_identifier") or out.get("ats_identifier")
            out["ats_resolution"]="verified_employer_source_match"
            out["employer_resolution_match_score"]=matched.get("resolver_match_score")
        else:
            employer_url,employer_desc=_resolve_employer_career_page(out)
            if employer_url:
                out["aggregator_url"]=out.get("aggregator_url") or out.get("original_url") or out.get("url")
                out["job_board_posted_at"]=next((out.get(field) for field in ("posted_at","posted_on","date_posted","datePosted","published_at","publication_date","updated_at") if out.get(field) not in (None,"")),None)
                out["original_url"]=employer_url
                provider,identifier=detect_ats(employer_url)
                if provider:
                    out["ats_provider"]=provider
                    out["ats_identifier"]=identifier
                out["ats_resolution"]="employer_career_page_canonical" if should_resolve_employer else "employer_career_page_fallback"
        if len(employer_desc)>len(resolved):resolved=employer_desc
    if len(resolved)>len(current):out["description"]=resolved
    final=(out.get("description") or "").strip()
    out["description_length"]=len(final)
    out["description_complete"]=_looks_like_complete_jd(final,source)
    out["description_usable"]=_looks_like_usable_jd(final,source)
    out["jd_signal_score"]=_jd_signal_score(final)
    out["jd_resolution_source"]="employer_career_page_canonical" if employer_url and should_resolve_employer else ("employer_career_page_fallback" if employer_url else ("jsonld_or_original_ats_public_job_detail_page" if len(resolved)>len(current) else "source_payload"))
    return out

def finalize_report(report_path,output_path="generated/finalized_jobs.json",hours=24,now=None,since=None):
    report=json.loads(Path(report_path).read_text(encoding="utf-8"));profile=load_profile()
    finalized=[];held=[]
    for item in report.get("results",[]):
        if item.get("action")!="ELIGIBLE_FOR_RESUME":continue
        raw=resolve_full_jd(item["job"])
        # Workday search results can contain a requisition that closes between
        # discovery and resume generation. Re-check the CXS detail endpoint here.
        if (raw.get("source") or "").lower()=="workday":
            url=raw.get("url") or raw.get("original_url") or ""
            m=re.search(r"https?://([^/]+)/(?:(?:[a-z]{2}-[A-Z]{2})/)?([^/]+)(/job/.+)",url)
            if m:
                host,site,external_path=m.groups()
                tenant=host.split(".",1)[0]
                live,_=job_detail_is_live(host,tenant,site,external_path)
                if not live:
                    held.append({"job":raw,"action":"REJECT_DEAD_JOB","reason":"Workday requisition no longer exists at the live detail endpoint.","diagnostics":{"url":url}})
                    continue
        # Every provider gets a final live-page check. Never spend resume
        # generation on a URL known to be dead, and never assume an unverifiable
        # application is live.
        application_url=raw.get("original_url") or raw.get("url") or ""
        is_approved_board = approved_board(raw.get("source"))
        if is_approved_board:
            application_url = board_application_url(raw)
            if not application_url:
                held.append({"job":raw,"action":"HOLD_BOARD_URL_UNVERIFIED","reason":"No valid original job-board application URL was provided."})
                continue
            raw["board_url"] = application_url
            raw["source_apply_url"] = application_url
            raw["original_url"] = application_url
            raw["application_route"] = BOARD_APPLICATION_ROUTE
            raw["posting_verification_scope"] = "JOB_BOARD_ONLY"
        if not is_approved_board and _is_aggregator_url(application_url):
            _mark_employer_resolution_pending(raw)
            held.append({"job":raw,"action":"HOLD_ATS_UNRESOLVED","reason":"Aggregator listing could not be resolved to an authoritative employer/ATS application page before resume generation.","diagnostics":{"url":application_url,"source":raw.get("source"),"ats_resolution":raw.get("ats_resolution")}})
            continue
        discovery_source=(raw.get("source") or "").lower()
        aggregator_origin=bool(raw.get("aggregator_url")) or discovery_source in JOB_BOARD_SOURCES
        check_now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if since:
            try:
                cutoff=datetime.fromisoformat(str(since).replace("Z","+00:00")).astimezone(timezone.utc)
            except (ValueError,TypeError):
                cutoff=check_now-timedelta(hours=hours)
        else:
            cutoff=check_now-timedelta(hours=hours)
        posting_fields=("posted_at","posted_on","postedDate","posted_date","date_posted","datePosted","published_at","publishedAt","publication_date","datePublished")
        raw["discovery_posted_at"]=raw.get("job_board_posted_at") or next((raw.get(field) for field in posting_fields if raw.get(field) not in (None,"")),None)
        selected_posted=None;selected_label=None
        if is_approved_board:
            # Source-native posted/updated dates are valid board evidence, but
            # MUST NOT be labeled as verified employer/ATS dates.
            selected_posted, board_basis = board_date(raw,check_now)
            selected_label = str(raw.get("posted_on") or raw.get("posted_at") or "")
            raw["freshness_basis"] = board_basis
        elif aggregator_origin:
            # The employer/ATS date has first priority whenever it is available.
            official_page=_fetch_public_page(application_url)
            selected_posted,selected_label=_official_posted_at(official_page,now=check_now)
        else:
            # Direct ATS jobs can gain a more authoritative posting field during
            # detail resolution after the earlier freshness pass. Re-check that
            # final value here so an old official posting cannot bypass the
            # current provider/global freshness window.
            from app.freshness import _parse_posting_value
            for field in posting_fields:
                value=raw.get(field)
                if value in (None,""):continue
                selected_posted=_parse_posting_value(value,check_now)
                if selected_posted is not None:
                    selected_label=str(value);break
        if selected_posted is not None:
            if is_approved_board:
                raw["board_posted_at"] = selected_posted.isoformat()
                raw["board_posted_label"] = selected_label
                # Legacy field is used only as timestamp transport by existing
                # dashboards; posting_verification_scope clearly distinguishes it.
                raw["official_posted_at"] = selected_posted.isoformat()
                raw["official_posted_label"] = selected_label
            else:
                raw["official_posted_at"]=selected_posted.isoformat()
                raw["official_posted_label"]=selected_label
                raw["freshness_basis"]="official_employer_posting_date"
        if is_approved_board and selected_posted is None:
            held.append({"job":raw,"action":"HOLD_BOARD_DATE_UNVERIFIED",
                         "reason":"Source-hosted job has no verifiable board date within the requested window."})
            continue
        if aggregator_origin and not is_approved_board and selected_posted is None:
            # Only the employer's official careers/ATS posting date can prove
            # freshness. Source-board timestamps are informational and cannot
            # substitute for the original employer posting date.
            held.append({
                "job": raw, "action": "HOLD_POST_DATE_UNVERIFIED",
                "reason": "Employer/ATS posting date could not be verified. Job-board posting age is not authoritative.",
                "diagnostics": {
                    "url": application_url, "discovery_source": raw.get("source"),
                    "ats_resolution": raw.get("ats_resolution"),
                    "discovery_posted_at": raw.get("discovery_posted_at")
                }
            })
            continue
        proof_posted_at=selected_posted.isoformat() if selected_posted is not None else raw.get("freshness_verified_posted_at")
        strict_production_proof=since is not None
        if not proof_posted_at and strict_production_proof:
            held.append({"job":raw,"action":"HOLD_POST_DATE_UNVERIFIED","reason":"No verifiable posting timestamp remained available at the final production gate.","diagnostics":{"url":application_url,"production_cutoff":cutoff.isoformat(),"freshness_basis":raw.get("freshness_basis")}})
            continue
        if proof_posted_at:
            raw["freshness_proof"]={
                "posted_at":proof_posted_at,
                "label":selected_label or raw.get("official_posted_label") or raw.get("posted_on") or raw.get("posted_at"),
                "basis":raw.get("freshness_basis"),
                "production_cutoff":cutoff.isoformat(),
                "checked_at":check_now.isoformat(),
                "recovery_scan":bool(raw.get("recovery_scan")),
                "discovery_window_hours":raw.get("discovery_window_hours"),
            }
            proof_dt=datetime.fromisoformat(str(proof_posted_at).replace("Z","+00:00")).astimezone(timezone.utc)
            if proof_dt<cutoff or proof_dt>check_now+timedelta(minutes=10):
                held.append({"job":raw,"action":"REJECT_STALE_OFFICIAL_POSTING","reason":("The job-board listing date is outside the requested freshness window." if is_approved_board else "The employer/ATS posting date is outside the requested freshness window. Job-board dates are not accepted at the final gate."),"diagnostics":{"url":application_url,"official_posted_at":proof_dt.isoformat(),"official_posted_label":selected_label,"freshness_basis":raw.get("freshness_basis"),"freshness_hours":hours,"production_cutoff":cutoff.isoformat(),"discovery_source":raw.get("source")}})
                continue
        if is_approved_board and (raw.get("source") or "").lower()=="himalayas":
            # Official source detail endpoint: browser pages may be bot-protected.
            live_status,live_reason,detail=himalayas_open_details(raw)
            if live_status is True:
                if detail.get("description") and len(_clean_html(detail["description"])) > len(raw.get("description") or ""):
                    raw["description"]=_clean_html(detail["description"])
                    raw["description_length"]=len(raw["description"])
                    raw["description_complete"]=_looks_like_complete_jd(raw["description"],raw.get("source"))
                    raw["description_usable"]=_looks_like_usable_jd(raw["description"],raw.get("source"))
                # The source API may supply a fresh expiry beyond the sample feed.
                if detail.get("expiryDate"):
                    raw["expiryDate"]=detail["expiryDate"]
        else:
            live_status,live_reason=_live_public_job_page(application_url)
        if live_status is False:
            held.append({"job":raw,"action":"REJECT_DEAD_JOB","reason":"Application page no longer exists or is explicitly closed.","diagnostics":{"url":raw.get("original_url") or raw.get("url"),"live_check":live_reason}})
            continue
        if live_status is None:
            held.append({"job":raw,"action":"HOLD_LIVE_STATUS_UNVERIFIED","reason":"Application page could not be verified as live before resume generation.","diagnostics":{"url":raw.get("original_url") or raw.get("url"),"live_check":live_reason}})
            continue
        raw["live_check"]={"passed":True,"reason":live_reason,"checked_at":check_now.isoformat(),"url":application_url}
        if not (raw.get("description_complete") or raw.get("description_usable") or _looks_like_usable_jd(raw.get("description"),raw.get("source"))):
            held.append({"job":raw,"action":"HOLD_ORIGINAL_JD_NOT_FOUND","reason":"A trustworthy complete/original job description could not be resolved safely.","diagnostics":{"description_length":raw.get("description_length",len(raw.get("description") or "")),"jd_signal_score":raw.get("jd_signal_score"),"jd_resolution_source":raw.get("jd_resolution_source"),"url":raw.get("original_url") or raw.get("url")}});continue
        raw["tailoring_mode"]="FULL_JD" if raw.get("description_complete") else "BASE_RESUME_CONSERVATIVE"
        eligibility=two_category_filter(raw,profile);ok,reasons=passes_hard_filters(raw,profile)
        if not eligibility.get("eligible") or not ok:
            held.append({"job":raw,"eligibility":eligibility,"action":"SKIP_FINAL_ELIGIBILITY","reasons":reasons,"diagnostics":{"description_length":raw.get("description_length",len(raw.get("description") or "")),"jd_signal_score":raw.get("jd_signal_score"),"jd_resolution_source":raw.get("jd_resolution_source")}});continue
        # Paid resume generation requires a known application route. A verified
        # external ATS is preferred. Dice-hosted jobs remain eligible for a
        # controlled Dice adapter; the adapter must inspect the Apply flow and
        # This pipeline verifies the job/application destination but does not
        # decide whether the separate Muse application system can automate it.
        source_supported=set(ALL_ATS_PROVIDERS)
        if is_approved_board:
            raw["application_route"]=BOARD_APPLICATION_ROUTE
            raw["ats_provider"]=None
            raw["posting_verification_scope"]="JOB_BOARD_ONLY"
        elif raw.get("ats_provider") in source_supported:
            raw["application_route"]="EXTERNAL_ATS"
        elif (raw.get("source") or "").lower()=="dice" and "dice.com" in (raw.get("original_url") or raw.get("url") or "").lower():
            raw["application_route"]="DICE"
            raw["ats_provider"]="dice"
        else:
            _mark_employer_resolution_pending(raw)
            held.append({"job":raw,"eligibility":eligibility,"action":"HOLD_ATS_UNRESOLVED","reason":"Application route could not be determined safely before paid resume generation.","diagnostics":{"description_length":raw.get("description_length",len(raw.get("description") or "")),"jd_signal_score":raw.get("jd_signal_score"),"jd_resolution_source":raw.get("jd_resolution_source"),"ats_resolution":raw.get("ats_resolution"),"url":raw.get("original_url") or raw.get("url")}})
            continue
        finalized.append({"job":raw,"eligibility":eligibility,"action":"FINAL_JD_VERIFIED"})
    result={"finalized":len(finalized),"held_or_rejected":len(held),"results":finalized,"rejections":held}
    out=Path(output_path);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    return result

if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser();p.add_argument("--report",default="generated/eligible_jobs.json");p.add_argument("--output",default="generated/finalized_jobs.json");a=p.parse_args()
    result=finalize_report(a.report,a.output);print(json.dumps({k:v for k,v in result.items() if k not in ("results","rejections")},indent=2))
    for i,x in enumerate(result["results"],1):
        j=x["job"];print(f"{i}. {j.get('company_key')} | {j.get('title')} | JD chars={j.get('description_length')} | {x['action']}")
    for x in result["rejections"]:
        j=x["job"];d=x.get("diagnostics",{})
        reason=x.get("reason") or "; ".join(x.get("reasons") or [])
        print(f"HOLD/SKIP: {j.get('company_key')} | {j.get('title')} | {x['action']} | JD chars={d.get('description_length')} | signals={d.get('jd_signal_score')} | source={d.get('jd_resolution_source')} | reason={reason}")
    print(f"Saved finalized jobs to {a.output}")