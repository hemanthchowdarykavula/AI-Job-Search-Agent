from __future__ import annotations
import re

NO_SPONSOR_PATTERNS=(
 "without visa sponsorship","without sponsorship","no visa sponsorship",
 "without current or future visa sponsorship","without current or future sponsorship",
 "visa sponsorship is not available","visa sponsorship not available","sponsorship is not available","sponsorship not available",
 "not provide visa sponsorship","does not provide visa sponsorship","do not provide visa sponsorship",
 "unable to sponsor","cannot sponsor","not eligible for sponsorship","must not require sponsorship",
 "will not sponsor","no sponsorship available","not offer sponsorship","does not intend to provide sponsorship","do not intend to provide sponsorship",
 "no current or future sponsorship","current or future sponsorship is not available",
 "cannot provide current or future sponsorship","will not provide sponsorship",
 "does not provide employer support or sponsorship","do not provide employer support or sponsorship",
 "without the need for employer support or sponsorship now or in the future",
 "without the need for employer support or sponsorship","immigration support or sponsorship now or in the future",
 "immigration related employment benefit",
 "not eligible for f1 opt","not eligible for f-1 opt",
 "not eligible for f1 stem opt","not eligible for f-1 stem opt",
 "f1 opt or stem opt not eligible","f-1 opt or stem opt not eligible",
 "no immigration support","does not provide immigration support","do not provide immigration support",
 "must not require employer support","cannot require employer support"
)
NO_SPONSOR_REGEX_PATTERNS=(
 r"\b(?:visa|employment visa|immigration)\s+sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)\b",
 r"\b(?:we|the company|this employer|employer)\s+(?:does|do|will)\s+not\s+(?:offer|provide)\s+(?:(?:employment\s+)?visa\s+|employment\s+|immigration\s+)?sponsorship\b",
 r"\b(?:cannot|unable to|will not)\s+(?:provide\s+)?(?:visa\s+|employment\s+)?sponsor(?:ship)?\b",
 r"\b(?:must|should)\s+(?:be\s+)?(?:authorized|eligible)\s+to\s+work\s+[^.;]{0,100}\bwithout\s+(?:current\s+or\s+future\s+)?(?:visa\s+)?sponsorship\b",
 r"\bwithout\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+|now\s+or\s+in\s+the\s+future\s+)?(?:employment\s+|visa\s+|immigration\s+)?sponsorship\b",
 r"\b(?:now\s+or\s+in\s+the\s+future|current\s+or\s+future)[^.;]{0,100}\bsponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)\b",
)
SPONSOR_POSITIVE_PATTERNS=(
 "visa sponsorship is available","sponsorship is available","we sponsor","will sponsor",
 "h-1b sponsorship","h1b sponsorship","employment visa sponsorship","visa transfer"
)
CITIZENSHIP_PATTERNS=(
 "u.s. citizenship is required","us citizenship is required",
 "u.s. citizenship required","us citizenship required",
 "must be a u.s. citizen","must be a us citizen","must be a united states citizen",
 "u.s. citizens only","us citizens only","united states citizens only",
 "must be a citizen of the united states"
)

def _clean(v): return re.sub(r"\s+"," ",(v or "").lower()).strip()

def experience_range(text: str):
    text=_clean(text)
    patterns=(
        r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands[- ]on\s+)?experience",
        r"(?:experience|experienced)\s+(?:of\s+)?(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:years?|yrs?)",
    )
    for pattern in patterns:
        m=re.search(pattern,text)
        if m:return int(m.group(1)),int(m.group(2))
    return None

def required_years(text: str):
    text=_clean(text)
    rng=experience_range(text)
    if rng:return rng[0]
    vals=[]
    patterns=(
      r"(?:minimum(?: of)?|min\.?|at least)\s+(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands[- ]on\s+)?experience",
      r"(?:requires?|required|requirement:?|qualifications?:?)\s+(?:a\s+)?(?:minimum(?: of)?\s+)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands[- ]on\s+)?experience",
      r"(\d{1,2})\s*\+\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands[- ]on\s+)?(?:[a-z0-9&/+.\-]+\s+){0,5}?experience",
      r"(\d{1,2})\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands[- ]on\s+)?experience\s+(?:required|minimum)",
      r"(?:experience\s+)?min(?:imum)?\.?\s+(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands[- ]on\s+)?(?:software\s+engineering\s+)?experience",
      r"(?:bachelor(?:'s|’s)?\s+degree|master(?:'s|’s)?\s+degree|degree)\s*\+?\s*(\d{1,2})\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?experience",
      r"(?:bachelor(?:'s|’s)?\s+degree|master(?:'s|’s)?\s+degree|degree)[^.;\n]{0,80}?(\d{1,2})\s*(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:[a-z0-9&/, .\-]+\s+){0,8}?experience",
    )
    for pattern in patterns:
        vals.extend(int(x) for x in re.findall(pattern,text))
    vals=[x for x in vals if 0 < x <= 15]
    return max(vals) if vals else None

def experience_check(job: dict, profile: dict) -> dict:
    full=f"{job.get('title','')} {job.get('description','')}"
    rng=experience_range(full);req=required_years(full)
    candidate=profile.get("candidate_experience_years",5)
    min_req=profile.get("preferences",{}).get("min_required_years",4)
    max_req=profile.get("preferences",{}).get("max_required_years",6)
    if req is None:
        return {"category":"EXPERIENCE_NOT_STATED","eligible":True,"required_years":None,"candidate_years":candidate,"configured_window":[min_req,max_req]}
    # A candidate who exceeds a posting's stated minimum still satisfies the
    # requirement.  The configured minimum is a preference/seniority signal, not
    # a hard rejection threshold.  Explicit junior/intern titles are handled by
    # the title-family filter, while requirements above the configured maximum
    # remain out of scope.
    eligible=req <= max_req
    if not eligible:
        category="EXPERIENCE_TOO_SENIOR"
    elif req < min_req:
        category="EXPERIENCE_BELOW_PREFERRED_MINIMUM"
    else:
        category="EXPERIENCE_ELIGIBLE"
    return {"category":category,"eligible":eligible,"required_years":req,"minimum_years":rng[0] if rng else req,"maximum_years":rng[1] if rng else None,"candidate_years":candidate,"configured_window":[min_req,max_req]}

def sponsorship_check(job: dict, profile: dict) -> dict:
    """Record sponsorship wording for diagnostics only; never gate eligibility."""
    text=_clean(f"{job.get('title','')} {job.get('description','')}")
    explicit_no_sponsorship=any(x in text for x in NO_SPONSOR_PATTERNS) or any(re.search(p,text,re.I) for p in NO_SPONSOR_REGEX_PATTERNS)
    if explicit_no_sponsorship:
        return {"category":"SPONSORSHIP_UNAVAILABLE_INFORMATIONAL","eligible":True,"evidence":"Posting states sponsorship is unavailable; this is informational only and does not affect eligibility."}
    if any(x in text for x in SPONSOR_POSITIVE_PATTERNS):
        return {"category":"SPONSORSHIP_AVAILABLE_INFORMATIONAL","eligible":True,"evidence":"Posting contains affirmative sponsorship language; this is informational only and does not affect eligibility."}
    return {"category":"SPONSORSHIP_NOT_STATED","eligible":True,"evidence":"Sponsorship is not stated; sponsorship does not affect eligibility."}

CLEARANCE_PATTERNS=(
 "ts/sci","top secret","secret clearance","active clearance",
 "security clearance required","must hold a security clearance",
 "must possess a security clearance","must have a security clearance",
 "eligible to obtain and maintain a security clearance",
 "eligibility to obtain and maintain a security clearance",
 "eligible to obtain and maintain an active clearance",
 "eligibility to obtain and maintain an active clearance",
 "eligible to obtain and maintain an active u.s. secret",
 "eligibility to obtain and maintain a u.s. security clearance",
 "public trust clearance required","active public trust",
 "top secret/sci","top secret/ sci","ts/sci/polygraph","top secret/sci/polygraph",
 "top secret sci polygraph","polygraph clearance"
)

CLEARANCE_REGEX_PATTERNS=(
 r"\brequires?\b[^.]{0,100}(?:candidate|applicant|employee|hired candidate)?[^.]{0,80}(?:to )?\b(?:have|hold|possess|obtain|maintain)\b[^.]{0,100}(?:security )?\bclearance\b",
 r"\b(?:have|hold|possess|obtain|maintain)\b[^.]{0,80}(?:\btop secret\b(?:/sci)?|\bts/sci\b|\bsecret\b|\bsci\b)[^.]{0,60}(?:\bclearance\b)?",
 r"\b(?:minimum|following|required)\b[^.]{0,100}\bclearance(?:\(s\))?\b[^.]{0,120}(?:\btop secret\b|\bts/sci\b|\bsecret\b|\bsci\b|\bpolygraph\b)",
 r"(?:\btop secret\b(?:/sci)?|\bts/sci\b|\bsecret\b|\bsci\b)[^.;]{0,80}(?:\bpolygraph\b|\bsecurity clearance\b|\bclearance required\b)",
)

def citizenship_check(job: dict, profile: dict) -> dict:
    text=_clean(f"{job.get('title','')} {job.get('description','')}")
    if any(x in text for x in CITIZENSHIP_PATTERNS):
        return {"category":"US_CITIZENSHIP_REQUIRED","eligible":False,"evidence":"Posting explicitly requires U.S. citizenship."}
    return {"category":"CITIZENSHIP_NOT_REQUIRED","eligible":True,"evidence":None}

def clearance_check(job: dict, profile: dict) -> dict:
    text=_clean(f"{job.get('title','')} {job.get('description','')}")
    if any(x in text for x in CLEARANCE_PATTERNS) or any(re.search(p,text,re.I) for p in CLEARANCE_REGEX_PATTERNS):
        return {"category":"CLEARANCE_REQUIRED","eligible":False,"evidence":"Posting explicitly requires a security/public-trust clearance."}
    return {"category":"CLEARANCE_NOT_REQUIRED","eligible":True,"evidence":None}

def two_category_filter(job: dict, profile: dict) -> dict:
    exp=experience_check(job,profile); sponsor=sponsorship_check(job,profile); citizenship=citizenship_check(job,profile); clearance=clearance_check(job,profile)
    # Sponsorship is informational only. It must never reject or hold a job.
    eligible=exp["eligible"] and citizenship["eligible"] and clearance["eligible"]
    return {"eligible":eligible,"experience":exp,"sponsorship":sponsor,"citizenship":citizenship,"clearance":clearance}