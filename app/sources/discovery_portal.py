from __future__ import annotations
import hashlib, html, json, re
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from app.approved_job_boards import approved_board, board_url_is_valid

UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36","Accept":"text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9"}
DE_TERMS=("data engineer","data engineering","data platform engineer","data infrastructure engineer","data pipeline engineer","data integration engineer","analytics engineer","big data engineer","etl engineer")

def _get(url:str,timeout:int=20)->str:
    with urlopen(Request(url,headers=UA),timeout=timeout) as r:
        return r.read().decode("utf-8","replace")

def _plain(v)->str:
    v=html.unescape(str(v or ""))
    v=re.sub(r"<script[\s\S]*?</script>"," ",v,flags=re.I)
    v=re.sub(r"<style[\s\S]*?</style>"," ",v,flags=re.I)
    return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",v)).strip()

def _jobpostings(body:str)->list[dict]:
    out=[]
    pat=r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    for raw in re.findall(pat,body,re.I|re.S):
        try:data=json.loads(html.unescape(raw.strip()))
        except Exception:continue
        rows=data if isinstance(data,list) else [data]
        for row in rows:
            if not isinstance(row,dict):continue
            nodes=[row]+(row.get("@graph") if isinstance(row.get("@graph"),list) else [])
            out.extend(x for x in nodes if isinstance(x,dict) and x.get("@type")=="JobPosting")
    return out

def _location(j:dict)->str|None:
    vals=[];locs=j.get("jobLocation") or []
    if isinstance(locs,dict):locs=[locs]
    for loc in locs if isinstance(locs,list) else []:
        if not isinstance(loc,dict):continue
        a=loc.get("address") or {}
        if isinstance(a,dict):
            vals.append(", ".join(str(a.get(k)) for k in ("addressLocality","addressRegion","addressCountry") if a.get(k)))
    if str(j.get("jobLocationType") or "").upper()=="TELECOMMUTE":vals.append("Remote")
    return " | ".join(x for x in vals if x) or None

def _parse_posted(value):
    if not value:return None
    try:return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception:return None

def _normalize(provider:str,j:dict,page_url:str,hours:float|None=None)->dict|None:
    title=_plain(j.get("title"));desc=_plain(j.get("description"))
    posted=_parse_posted(j.get("datePosted"))
    if hours is not None and posted is not None and posted < datetime.now(timezone.utc)-timedelta(hours=float(hours)):return None
    if not any(t in (title+" "+desc[:2500]).lower() for t in DE_TERMS):return None
    org=j.get("hiringOrganization")
    company=_plain(org.get("name")) if isinstance(org,dict) else _plain(org)
    url=urljoin(page_url,str(j.get("url") or page_url))
    ident=j.get("identifier")
    if isinstance(ident,dict):ident=ident.get("value") or ident.get("name")
    stable=str(ident or url or company+":"+title);sid=hashlib.sha1(stable.encode()).hexdigest()[:20]
    # Keep the exact board-hosted detail URL for supported manual-apply boards.
    if approved_board(provider) and not board_url_is_valid(provider,url):
        return None
    return {"external_id":f"{provider}:{sid}","source":provider,"company_key":company or "Unknown","title":title,
      "location":_location(j),"employment_type":j.get("employmentType"),"url":url,"original_url":url,
      "description":desc,"description_complete":bool(desc),"updated_at":j.get("datePosted"),"posted_on":j.get("datePosted"),
      "valid_through":j.get("validThrough"),"discovery_only":True,"authoritative_source":False,"portal_source":provider,
      "board_listing_verified":approved_board(provider),"source_credit":provider.replace("_"," ").title() if approved_board(provider) else None}

def _page_fallback(provider:str,body:str,page_url:str)->dict|None:
    # Some public boards render useful job detail pages without JobPosting JSON-LD.
    # Use conservative title/company extraction; rows remain discovery-only.
    tm=re.search(r"<h1[^>]*>(.*?)</h1>",body,re.I|re.S) or re.search(r"<title[^>]*>(.*?)</title>",body,re.I|re.S)
    title=_plain(tm.group(1)) if tm else ""
    text=_plain(body)
    if not any(t in (title+" "+text[:5000]).lower() for t in DE_TERMS):return None
    company=""
    for pat in (r'"hiringOrganization"\s*:\s*\{[^{}]*"name"\s*:\s*"([^"]+)"',r'"company"\s*:\s*\{[^{}]*"name"\s*:\s*"([^"]+)"'):
        m=re.search(pat,body,re.I|re.S)
        if m:company=_plain(m.group(1));break
    sid=hashlib.sha1(page_url.encode()).hexdigest()[:20]
    return {"external_id":f"{provider}:{sid}","source":provider,"company_key":company or "Unknown","title":title,
      "location":None,"employment_type":None,"url":page_url,"original_url":page_url,"description":text[:12000],
      "description_complete":False,"updated_at":None,"posted_on":None,"valid_through":None,
      "discovery_only":True,"authoritative_source":False,"portal_source":provider,
      "board_listing_verified":approved_board(provider),"source_credit":provider.replace("_"," ").title() if approved_board(provider) else None}

def _pagination_links(body:str,page_url:str)->list[str]:
    """Return conservative same-host pagination links from a portal results page."""
    host=urlsplit(page_url).netloc.lower();out=[]
    for href in re.findall(r"""href=["']([^"']+)["']""",body,re.I):
        u=urljoin(page_url,html.unescape(href));p=urlsplit(u)
        if p.netloc.lower()!=host:continue
        tag_match=re.search(r"""<a[^>]*href=["']"""+re.escape(href)+r"""["'][^>]*>(.*?)</a>""",body,re.I|re.S)
        label=_plain(tag_match.group(1)) if tag_match else ""
        low=(href+" "+label).lower()
        if re.search(r'(?:[?&](?:page|p|offset)=\\d+|/page/\\d+)',href,re.I) or re.search(r'\\b(next|older|more)\\b',low):
            if u not in out:out.append(u)
    return out

def _numbered_page_url(url:str,page:int)->str:
    p=urlsplit(url);q=dict(parse_qsl(p.query,keep_blank_values=True));q["page"]=str(page)
    return urlunsplit((p.scheme,p.netloc,p.path,urlencode(q),p.fragment))

def fetch_jobs(provider:str,search_url:str,job_url_pattern:str,timeout:int=20,hours:float|None=None,max_detail_pages:int=250,max_search_pages:int=10)->list[dict]:
    """Crawl a public portal strictly as discovery; authoritative resolution is mandatory downstream."""
    out=[];seen=set();rx=re.compile(job_url_pattern,re.I);links=[]
    queue=[search_url];visited_pages=set()
    # Wellfound's public role pages use ?page=N but do not consistently expose
    # pagination anchors in the HTML returned to the crawler.
    if provider.lower()=="wellfound":
        queue.extend(_numbered_page_url(search_url,n) for n in range(2,max_search_pages+1))
    while queue and len(visited_pages)<max_search_pages and len(links)<max_detail_pages:
        page_url=queue.pop(0)
        if page_url in visited_pages:continue
        try:body=_get(page_url,timeout)
        except Exception:continue
        visited_pages.add(page_url)
        for j in _jobpostings(body):
            row=_normalize(provider,j,page_url,hours)
            if row and row["external_id"] not in seen:seen.add(row["external_id"]);out.append(row)
        for href in re.findall(r"""href=["']([^"']+)["']""",body,re.I):
            u=urljoin(page_url,html.unescape(href))
            if rx.search(u) and u not in links:links.append(u)
        for u in _pagination_links(body,page_url):
            if u not in visited_pages and u not in queue:queue.append(u)
    for u in links[:max_detail_pages]:
        try:detail=_get(u,timeout)
        except Exception:continue
        found=False
        for j in _jobpostings(detail):
            row=_normalize(provider,j,u,hours)
            if row:
                found=True
                if row["external_id"] not in seen:seen.add(row["external_id"]);out.append(row)
        if not found:
            row=_page_fallback(provider,detail,u)
            if row and row["external_id"] not in seen:seen.add(row["external_id"]);out.append(row)
    print(f"DiscoveryPortal / {provider}: {len(out)} DE jobs from {len(visited_pages)} search page(s), {len(links)} detail link(s)",flush=True)
    return out
