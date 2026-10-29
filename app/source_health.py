from __future__ import annotations
import argparse, json, re, time
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from app.sources.career_site import validate_source as validate_career_site
from app.discovery import DIRECT_PROVIDERS, FALLBACK_ATS_PROVIDERS
from app.source_registry import load_registry, as_discovery_config

ROOT=Path(__file__).resolve().parents[1]
UA={"User-Agent":"Mozilla/5.0","Accept":"application/json,text/html,*/*"}

def _normalize_probe_url(url: str) -> str:
    """Return an HTTP-safe URL without corrupting already escaped characters."""
    parts=urlsplit(str(url).strip())
    host=parts.hostname.encode("idna").decode("ascii") if parts.hostname else ""
    if parts.port:
        host=f"{host}:{parts.port}"
    if parts.username:
        auth=quote(parts.username,safe="")
        if parts.password is not None:
            auth += ":" + quote(parts.password,safe="")
        host=f"{auth}@{host}"
    path=quote(parts.path,safe="/%:@!$&'()*+,;=-._~")
    query=quote(parts.query,safe="=&?/:;+,%@!$'()*-._~")
    fragment=quote(parts.fragment,safe="=&?/:;+,%@!$'()*-._~")
    return urlunsplit((parts.scheme,host,path,query,fragment))

def _probe(url: str, timeout: int=12, method: str="GET", data: bytes|None=None, headers: dict|None=None, retries: int=2) -> dict:
    """Health probe with one bounded retry for transient network/provider errors."""
    h=dict(UA); h.update(headers or {})
    safe_url=_normalize_probe_url(url)
    last=None
    for attempt in range(max(1,retries)):
        try:
            with urlopen(Request(safe_url,data=data,headers=h,method=method),timeout=timeout) as resp:
                return {"status":"ok","http_status":getattr(resp,"status",None),"attempts":attempt+1}
        except HTTPError as exc:
            last=exc
            if exc.code in (404,410):
                return {"status":"broken","http_status":exc.code,"error":str(exc),"attempts":attempt+1}
            if exc.code not in (408,425,429,500,502,503,504):
                return {"status":"blocked_or_http_error","http_status":exc.code,"error":str(exc),"attempts":attempt+1}
        except (IncompleteRead,URLError,TimeoutError,OSError,UnicodeError,ValueError) as exc:
            last=exc
        if attempt+1<max(1,retries):
            time.sleep(0.35*(2**attempt))
    if isinstance(last,HTTPError):
        return {"status":"blocked_or_http_error","http_status":last.code,"error":str(last),"attempts":max(1,retries)}
    return {"status":"unreachable","http_status":None,"error":str(last) if last else "probe failed","attempts":max(1,retries)}

def _row(provider, company, target, result, **extra):
    return {"provider":provider,"company":company,"target":target,**result,**extra}

def run(path: str="data/job_sources.json", timeout: int=12) -> dict:
    cfg=json.loads((ROOT/path).read_text(encoding="utf-8"))
    # Health must audit the same effective universe discovery can execute:
    # configured seeds plus persistent sources learned from employer enrichment.
    learned=as_discovery_config(load_registry())
    for provider, units in learned.items():
        if not isinstance(units,list):
            continue
        existing=cfg.get(provider)
        if not isinstance(existing,list):
            existing=[]
        seen={json.dumps(x,sort_keys=True,default=str) for x in existing if isinstance(x,dict)}
        for unit in units:
            key=json.dumps(unit,sort_keys=True,default=str)
            if key not in seen:
                existing.append(unit);seen.add(key)
        cfg[provider]=existing
    rows=[]
    # Career-site validation is network-bound and independent per employer.
    # Validate in parallel so a few slow/blocked sites do not serialize the
    # entire production health gate.
    career_units=list(cfg.get("career_site",[]))
    def _validate_career(x):
        r=validate_career_site(x["company"],x["search_url"],x["job_url_pattern"],timeout)
        effective=r["status"]
        if effective=="ok" and r.get("matching_job_links",0)==0: effective="no_crawlable_links"
        r["effective_status"]=effective
        return _row("career_site",x["company"],x["search_url"],r)
    if career_units:
        with ThreadPoolExecutor(max_workers=min(16,len(career_units))) as pool:
            rows.extend(pool.map(_validate_career,career_units))
    for x in cfg.get("greenhouse",[]):
        url=f'https://boards-api.greenhouse.io/v1/boards/{x["board_token"]}/jobs'
        rows.append(_row("greenhouse",x.get("company") or x["board_token"],url,_probe(url,timeout)))
    for x in cfg.get("lever",[]):
        url=f'https://api.lever.co/v0/postings/{x["site"]}?mode=json&limit=1'
        rows.append(_row("lever",x.get("company") or x["site"],url,_probe(url,timeout)))
    for x in cfg.get("ashby",[]):
        board_name=quote(str(x["board_name"]).strip(),safe="")
        url=f'https://api.ashbyhq.com/posting-api/job-board/{board_name}'
        rows.append(_row("ashby",x.get("company") or x["board_name"],url,_probe(url,timeout)))
    for x in cfg.get("smartrecruiters",[]):
        ident=x.get("company_identifier") or x.get("identifier")
        url=f'https://api.smartrecruiters.com/v1/companies/{ident}/postings?limit=1&offset=0'
        rows.append(_row("smartrecruiters",x.get("company") or ident,url,_probe(url,timeout)))
    for x in cfg.get("workday",[]):
        url=f'https://{x["host"].strip("/")}/wday/cxs/{x["tenant"]}/{x["site"]}/jobs'
        body=json.dumps({"appliedFacets":{},"limit":1,"offset":0,"searchText":"Data Engineer"}).encode()
        rows.append(_row("workday",x.get("company") or x["tenant"],url,_probe(url,timeout,method="POST",data=body,headers={"Content-Type":"application/json"})))
    for provider in ("successfactors","icims","oracle"):
        for x in cfg.get(provider,[]):
            url=x.get("base_url") or x.get("search_url") or x.get("careers_url") or x.get("original_url")
            if not url:
                rows.append(_row(provider,x.get("company") or provider,"",{"status":"configured","http_status":None},coverage_status="CONFIGURED"))
                continue
            rows.append(_row(provider,x.get("company") or provider,url,_probe(url,timeout)))
    for x in cfg.get("eightfold",[]):
        url=x.get("careers_url") or x.get("search_url") or x.get("base_url") or x.get("original_url")
        if not url:
            rows.append(_row("eightfold",x.get("company") or "eightfold","",{"status":"configured","http_status":None},coverage_status="CONFIGURED"))
            continue
        rows.append(_row("eightfold",x.get("company") or "eightfold",url,_probe(url,timeout)))
    # Dedicated collectors beyond the original API-backed set still need a
    # board-level health row for every configured tenant.
    for provider in ("ukg","ultipro","ultipro_ukg","adp_workforce_now","avature","phenom","paylocity","workable","jazzhr","jazzhr_alt","dayforce","cornerstone","jobvite"):
        for x in cfg.get(provider,[]):
            url=x.get("search_url") or x.get("base_url") or x.get("careers_url") or x.get("original_url")
            if not url:
                rows.append(_row(provider,x.get("company") or provider,"",{"status":"configured","http_status":None},coverage_status="CONFIGURED"))
                continue
            probe=_probe(url,timeout)
            coverage="DIRECT" if probe.get("status")=="ok" else "BLOCKED"
            rows.append(_row(provider,x.get("company") or provider,url,probe,coverage_status=coverage))
    # Official supplemental feeds and public discovery boards are independently
    # reported as configured. Actual success/failure and job counts come from
    # the collector's per-run health log; do not burn extra API quota probing.
    for provider,label in (("hiringcafe","HiringCafe"),("himalayas","Himalayas")):
        spec=cfg.get(provider) or {}
        if isinstance(spec,dict) and spec.get("enabled",False):
            rows.append(_row(provider,label,spec.get("endpoint") or "",
                             {"status":"configured","http_status":None},
                             coverage_status="DISCOVERY_ADAPTER",
                             collector_class="official_provider_api"))
    for spec in cfg.get("discovery_portal",[]):
        if isinstance(spec,dict) and spec.get("enabled",True):
            rows.append(_row(spec.get("provider"),spec.get("company") or spec.get("provider"),
                             spec.get("search_url") or "",
                             {"status":"configured","http_status":None},
                             coverage_status="DISCOVERY_ADAPTER",
                             collector_class="discovery_only_public_board"))
    # Every configured ATS family must appear in source health.  For providers
    # without a dedicated API probe yet, probe the configured public board and
    # label it fallback/configured rather than silently omitting it.
    dedicated={"career_site","greenhouse","lever","ashby","smartrecruiters","workday","successfactors","icims","oracle","eightfold","ukg","ultipro","ultipro_ukg","adp_workforce_now","avature","phenom","paylocity","workable","jazzhr","jazzhr_alt","dayforce","cornerstone","jobvite","dice","ziprecruiter"}
    for provider, units in cfg.items():
        if provider in dedicated or not isinstance(units,list):
            continue
        for x in units:
            if not isinstance(x,dict):
                continue
            url=x.get("search_url") or x.get("base_url") or x.get("careers_url") or x.get("original_url")
            if not url:
                rows.append(_row(provider,x.get("company") or provider,"",{"status":"configured","http_status":None},coverage_status="CONFIGURED"))
                continue
            probe=_probe(url,timeout)
            if probe.get("status")=="ok":
                coverage="DIRECT_PUBLIC_BOARD" if provider in DIRECT_PROVIDERS else "FALLBACK"
            else:
                coverage="BLOCKED"
            rows.append(_row(provider,x.get("company") or provider,url,probe,coverage_status=coverage,collector_class=("direct_public_board" if provider in DIRECT_PROVIDERS else "fallback")))
    # Surface provider families with no configured tenant instead of silently
    # omitting them from the health report. This separates collector support
    # from actual production coverage.
    for provider in DIRECT_PROVIDERS:
        units=cfg.get(provider,[])
        if isinstance(units,list) and not units:
            collector_class="native_client_scoped" if provider in {"talentreef","jobappnetwork"} else "unseeded"
            rows.append(_row(
                provider, provider, "",
                {"status":"unseeded","http_status":None},
                coverage_status="UNSEEDED",
                collector_class=collector_class,
            ))
    counts={}
    for r in rows:
        s=r.get("effective_status") or r["status"]
        counts[s]=counts.get(s,0)+1
    coverage_counts={}
    for r in rows:
        k=r.get("coverage_status") or ("DIRECT" if (r.get("effective_status") or r.get("status"))=="ok" else "UNKNOWN")
        coverage_counts[k]=coverage_counts.get(k,0)+1
    return {"counts":counts,"coverage_counts":coverage_counts,"provider_classes":{"direct":list(DIRECT_PROVIDERS),"fallback":list(FALLBACK_ATS_PROVIDERS)},"sources":rows}

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--sources",default="data/job_sources.json")
    p.add_argument("--timeout",type=int,default=12)
    p.add_argument("--output",default="state/source_health.json")
    a=p.parse_args()
    report=run(a.sources,a.timeout)
    out=ROOT/a.output; out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report["counts"],indent=2))
    for r in report["sources"]:
        status=r.get("effective_status") or r["status"]
        if status!="ok":
            print(f'{status:22} [{r["provider"]}] {r["company"]}: {r["target"]} http={r.get("http_status")} links={r.get("matching_job_links","")}')
    print(f"Saved report to {out}")
