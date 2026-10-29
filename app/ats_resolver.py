from __future__ import annotations
import re
from urllib import request
from urllib.parse import urljoin, urlsplit, unquote
import json
from app.source_registry import detect_ats

ATS_HOST_HINTS=("greenhouse.io","lever.co","ashbyhq.com","smartrecruiters.com","myworkdayjobs.com","myworkdaysite.com","icims.com","jobvite.com","dayforcehcm.com","dayforce.com","ultipro.com","recruiting.com","workforcenow.adp.com","jobs.adp.com","successfactors.com","successfactors.eu","oraclecloud.com","eightfold.ai","phenompeople.com","workable.com","teamtailor.com","recruitee.com","bamboohr.com","breezy.hr","rippling.com","pinpointhq.com","careerplug.com","freshteam.com","jobscore.com","personio.com","personio.de","comeet.com","neogov.com","governmentjobs.com","applicantpro.com","fountain.com","hirebridge.com","zohorecruit.com","zohorecruit.eu","zohorecruit.in","manatal.com","careers-page.com","join.com","applitrack.com","frontlineeducation.com","hireology.com","paycor.com","recruitingbypaycor.com","peopleadmin.com","isolvedhire.com","isolved.com","hibob.com","gohire.io","hiringthing.com","homerun.co","pageuppeople.com","dover.com","gem.com","polymer.co","hirehive.com","deel.com","applicantstack.com","ceipal.com","trakstar.com","taleo.net","brassring.com","paycomonline.net","bullhornstaffing.com","jobdiva.com","clearcompany.com","csod.com","applytojob.com","kula.ai","rival-hr.com","werecruit.io","werecruit.com","firststage.co","recruiterbox.com","talentbrew.com","radancy.com","paradox.ai","schooljobs.com","higheredjobs.com","talentreef.com","jobappnetwork.com","myworkchoice.com")
AGGREGATOR_HOSTS=("dice.com","indeed.com","linkedin.com","ziprecruiter.com","monster.com","wellfound.com","builtin.com","ycombinator.com","adzuna.com","glassdoor.com","simplyhired.com","careerbuilder.com","hiringcafe.com","himalayas.app","welcometothejungle.com")

def _is_aggregator_url(url):
 try:
  host=(urlsplit(url or "").netloc or "").lower()
 except Exception:
  return False
 return any(host==x or host.endswith("."+x) for x in AGGREGATOR_HOSTS)

def _job_direct_apply_candidates(job):
 keys=("external_apply_url","externalApplyUrl","application_url","applicationUrl","apply_url","applyUrl","job_apply_url","jobApplyUrl","employer_job_url","employerJobUrl")
 out=[]
 for key in keys:
  value=job.get(key)
  if isinstance(value,str) and value.startswith(("http://","https://")) and not _is_aggregator_url(value):
   out.append(value)
 original=job.get("original_url")
 if isinstance(original,str) and original.startswith(("http://","https://")) and not _is_aggregator_url(original):
  out.insert(0,original)
 return list(dict.fromkeys(out))

APPLY_KEY_RE=re.compile(r'(?i)(?:external)?apply(?:url|link)|application(?:url|link)|redirect(?:url|link)|applyUrl')

def _fetch(url):
 if not url:return ""
 try:
  req=request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; AI-Job-Search-Agent/1.0)"})
  with request.urlopen(req,timeout=20) as resp:return resp.read().decode("utf-8",errors="replace")
 except Exception:return ""

def _same_company_hint(job,url):
 company=re.sub(r"[^a-z0-9]","",(job.get("company_key") or job.get("company") or "").lower())
 host=re.sub(r"[^a-z0-9]","",urlsplit(url).netloc.lower())
 return not company or company[:8] in host or any(x in url.lower() for x in re.findall(r"[a-z0-9]{4,}",(job.get("company_key") or "").lower())[:2])

def _looks_like_static_asset_url(url):
 try:
  parsed=urlsplit(url or "")
  host=(parsed.netloc or "").lower()
  path=(parsed.path or "").lower().split("?",1)[0]
 except Exception:
  return True
 static_suffixes=(".css",".js",".mjs",".map",".png",".jpg",".jpeg",".gif",".svg",".ico",".woff",".woff2",".ttf",".eot",".pdf")
 if path.endswith(static_suffixes):
  return True
 if host.startswith(("static.","cdn.","assets.","rmkcdn.")):
  return True
 return False

def _candidate_links(page,base):
 links=[]
 # Aggregators frequently hide the employer ATS URL in JSON/script state instead
 # of a clickable anchor. Normalize escaped slashes and inspect both forms.
 value=(page or "").replace(r"\/", "/").replace(r"\u002F", "/")
 raw=[]
 raw.extend(re.findall(r'''(?i)href=["']([^"'#]+)["']''',value))
 raw.extend(re.findall(r'''(?i)https?://[^"'<>\\\s]+''',value))
 # Prefer URLs explicitly stored in apply/application/redirect JSON properties.
 raw.extend(m.group(1) for m in re.finditer(
  r'''(?i)(?:external)?apply(?:url|link)|application(?:url|link)|redirect(?:url|link)'''+
  r'''[^:]{0,30}:\s*["'](https?://[^"']+)["']''', value))
 for href in raw:
  # Discovery/aggregator pages can contain malformed pseudo-URLs in script
  # state (for example unmatched IPv6 brackets). One bad candidate must not
  # abort resolution for every other job in the production cycle.
  try:
   u=unquote(urljoin(base,href)).rstrip("),.;")
   urlsplit(u)  # validate bracketed netlocs before downstream provider checks
  except (ValueError, TypeError):
   continue
  if _looks_like_static_asset_url(u):continue
  if any(host in u.lower() for host in ATS_HOST_HINTS):links.append(u)
 return list(dict.fromkeys(links))

def _organization_urls(page,base):
 """Extract structured hiring-organization identity URLs from a job page."""
 out=[]
 for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',page or "",re.I|re.S):
  try:data=json.loads(raw)
  except Exception:continue
  stack=data if isinstance(data,list) else [data]
  while stack:
   obj=stack.pop()
   if isinstance(obj,list):stack.extend(obj);continue
   if not isinstance(obj,dict):continue
   typ=str(obj.get("@type") or "").lower()
   if "jobposting" in typ:
    org=obj.get("hiringOrganization")
    if isinstance(org,dict):
     for key in ("sameAs","url"):
      value=org.get(key)
      values=value if isinstance(value,list) else [value]
      for item in values:
       if isinstance(item,str) and item.strip():
        try:
         u=urljoin(base,item.strip())
         if urlsplit(u).scheme in {"http","https"}:out.append(u)
        except Exception:continue
   for value in obj.values():
    if isinstance(value,(dict,list)):stack.append(value)
 return list(dict.fromkeys(out))

def resolve_original_ats(job):
 """Best-effort resolution from aggregator/detail URL to an employer ATS URL; no LLM and no application action."""
 out=dict(job)
 # Prefer an explicit non-aggregator apply destination supplied by the source
 # payload. This is stronger evidence than rediscovering the URL from HTML.
 for direct in _job_direct_apply_candidates(job):
  provider,identifier=detect_ats(direct)
  if provider:
   out.update({"original_url":direct,"ats_provider":provider,"ats_identifier":identifier,"ats_resolution":"direct_apply_metadata"})
   if _is_aggregator_url(job.get("url") or ""):out["aggregator_url"]=job.get("url")
   return out
 start=job.get("original_url") or job.get("url") or ""
 out["original_url"]=start
 provider,identifier=detect_ats(start)
 if provider:
  out.update({"original_url":start,"ats_provider":provider,"ats_identifier":identifier,"ats_resolution":"direct"})
  return out
 page=_fetch(start)

 # Many SuccessFactors tenants are hosted on the employer's branded domain.
 # If the submitted page itself is a job detail and loads SuccessFactors career
 # assets, preserve that authoritative employer URL instead of mistaking a CDN
 # stylesheet or an embedded chatbot vendor for the job destination.
 try:
  start_path=(urlsplit(start).path or "").lower()
 except Exception:
  start_path=""
 if (
  not _is_aggregator_url(start)
  and "/job/" in start_path
  and "successfactors" in (page or "").lower()
 ):
  out.update({
   "original_url":start,
   "ats_provider":"successfactors",
   "ats_identifier":(urlsplit(start).netloc or "").lower(),
   "ats_resolution":"branded_successfactors_job_page",
  })
  return out

 org_urls=[u for u in _organization_urls(page,start) if not _is_aggregator_url(u)]
 if org_urls:out["organization_url_evidence"]=org_urls[0]
 links=_candidate_links(page,start)
 candidates=[]
 for link in links:
  provider,identifier=detect_ats(link)
  if provider:candidates.append((0 if _same_company_hint(job,link) else 1,link,provider,identifier))
 for _,link,provider,identifier in sorted(candidates,key=lambda x:x[0]):
  out.update({"original_url":link,"ats_provider":provider,"ats_identifier":identifier,"ats_resolution":"linked_from_discovery_page"})
  return out
 out["ats_resolution"]="unresolved"
 return out
