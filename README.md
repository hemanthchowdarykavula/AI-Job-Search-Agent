# AI Job Search Agent

A production-oriented U.S. Data Engineering job-search system that discovers jobs, applies strict eligibility rules, retrieves complete job descriptions, generates JD-specific resumes, validates resume quality, builds a manual application queue, and syncs the queue to a persistent hosted dashboard.

## Current production flow

```text
Scheduled cloud trigger
→ broad multi-source discovery
→ freshness + deduplication
→ U.S.-only / Data-Engineering-family eligibility
→ Full-Time / W-2 eligibility
→ sponsorship / citizenship / clearance checks
→ complete original JD retrieval and verification
→ final eligibility
→ JD-driven resume generation
→ ATS + evidence + recruiter + human-quality validation
→ targeted retry when a validation gate fails
→ validated DOCX/PDF artifacts
→ READY_TO_APPLY
→ persistent application queue
→ hosted dashboard sync
→ user manually opens employer application and submits
→ user marks application Applied
→ applied status + resume reference remain persistent
```

Application submission is intentionally manual. The production agent prepares qualified applications and resumes but does not autonomously submit employer applications.

## Production schedule and reliability

Production runs Monday-Friday in **America/New_York** at seven two-hour slots:

**7 AM, 9 AM, 11 AM, 1 PM, 3 PM, 5 PM, and 7 PM Eastern.**

Each intended slot now has **six independent GitHub Actions trigger opportunities at :07, :17, :27, :37, :47, and :57**. The :07 event is the primary trigger. Every later event is a watchdog: it checks whether another run in that slot is already queued, running, or successful and exits as a no-op when the slot is healthy. If the earlier trigger was dropped or the run failed, the next watchdog executes recovery. This substantially reduces dependence on any single scheduled event.

Persistent scheduler and per-source watermarks provide another recovery layer. A missed or failed discovery interval is resumed from the last successful watermark instead of silently advancing past it. Workday tenants maintain independent watermarks so one failing tenant does not prevent healthy tenants from advancing.

The reliability model is therefore:

```text
Primary scheduled trigger
→ watchdog recovery
→ persistent watermark catch-up
```

## Eligibility policy

The production pipeline targets U.S. Data Engineering roles, including Data Engineer, Senior Data Engineer, Staff Data Engineer, Principal Data Engineer, cloud-focused Data Engineer roles, and legitimate closely related individual-contributor Data Engineering roles.

Hard filtering includes:

- United States roles only. Onsite, hybrid, and U.S.-scoped remote roles are allowed.
- Data Engineering role family only.
- Full-time / W-2 focus.
- Reject when the posting explicitly says current or future employment sponsorship is unavailable.
- Reject explicit U.S.-citizenship requirements.
- Reject roles requiring a security clearance.
- Reject non-IC Manager, Director, Architect, and Consultant role families even when the title contains Data Engineering terminology.
- Unknown sponsorship status is not automatically rejected.

## Discovery

Discovery is employer/ATS-first and open-ended rather than an allowlist.

- 82 recognized/configurable ATS and career-site provider families are supported by the source registry.
- The static source catalog is seed coverage, not the complete employer universe.
- At the 7 AM Eastern production slot, the slow enrichment layer expands the U.S. employer universe from public/authoritative company feeds, resolves evidence-backed official domains, finds employer career pages, detects hosted or embedded ATS platforms, and persists learned sources.
- Fast production cycles merge configured seeds with those learned sources before collecting current jobs.
- Direct employer career pages and ATS boards are primary. Dice and ZipRecruiter are supplemental discovery. Other aggregators remain supplemental/authorized-only unless an executable adapter exists.
- Aggregator discoveries must resolve to a complete authoritative employer/ATS posting before eligibility, resume generation, or application.
- Source health audits both configured and persistently learned sources.
- Unknown employers are allowed; target-company lists are priorities/examples, never hard allowlists.
- Fidelity Investments, Cigna Healthcare, and Target Corporation are explicit hard exclusions from eligibility/application even if broad discovery sees their jobs.

Discovery state is persisted across cloud runs so providers resume from their own successful watermarks rather than relying on a single global time window.

## JD-specific resume generation

The complete verified job description is the primary source for the tailored resume's technical content.

The master candidate profile is used as the factual identity and chronology anchor for fixed information such as contact information, employers/clients, job titles, locations, employment dates, education, and existing certifications.

For each eligible job, the resume generator:

1. extracts the material requirements, technologies, tools, platforms, and responsibilities from the JD;
2. prioritizes those requirements in the Summary and Technical Skills;
3. demonstrates important JD technologies and responsibilities naturally within relevant experience sections while preserving employer/domain context;
4. preserves fixed chronology and identity facts;
5. does not invent employers, titles, dates, locations, education, certifications, unsupported numerical metrics, or named business/project outcomes.

The system does **not** use the master resume's existing technical bullet content as a whitelist for JD tailoring.

## Resume validation

Generated resumes pass through automated quality gates before they can enter the application queue. Validation covers JD requirement coverage, ATS-oriented structure/coverage, experience depth, evidence consistency, recruiter readability, human-quality checks, and DOCX/PDF artifact validation.

Failed resumes are retried against the specific failed gates rather than blindly regenerated. A job reaches `READY_TO_APPLY` only after the required resume and artifact checks pass.

There is no artificial production resume-count cap: all jobs that qualify in a production cycle can proceed through resume generation and validation, subject to normal runtime/API constraints.

## Hosted application dashboard

The persistent hosted dashboard displays the application pipeline and remains available independently of the user's laptop.

For each queued application it can show:

- company and role
- application status
- source / portal
- validated resume PDF
- employer application link
- applied date/status

The user opens **Open job / Apply**, completes the employer application manually, and then selects **Mark Applied**. Applied status is persisted independently of subsequent production syncs. The associated resume reference is also preserved, with recovery support for older applied records whose current ledger entry no longer contains the PDF path.

## Automation boundary

The automated production boundary is:

```text
Discover
→ qualify
→ retrieve/verify JD
→ generate + validate resume
→ queue
→ sync dashboard
```

The final employer submission remains a user action. The system does not run autonomous browser submission agents.

## State and cloud operation

Scheduled production runs in GitHub Actions. Generated scheduler state, discovery state, ledger data, resume artifacts, and queue data are carried across runs and synchronized to the hosted dashboard. The dashboard stores its persistent state on its attached volume, so normal service deployments do not intentionally reset application history.

The laptop does not need to remain on for scheduled discovery, resume generation, queue creation, dashboard synchronization, or watchdog recovery.

## Safety and accuracy

The system must not fabricate fixed candidate facts, employers, chronology, education, certifications, numerical achievements, or screening-question answers. Eligibility decisions preserve explicit evidence from the posting, and failed source scans retain their previous watermark so transient provider failures do not silently create discovery gaps.

See **[PROJECT_STATUS.md](PROJECT_STATUS.md)** for implementation-level status and historical validation details.

## Two independent job input paths

The project now supports two independent ways to reach the same resume-generation pipeline:

1. **Job Discovery** — the existing automated production path. It discovers jobs, applies the configured job-family/location/experience and other production gates, resolves the authoritative JD, and then prepares validated resume artifacts.
2. **Manual Job Links** — paste one or many job URLs into the hosted dashboard at `/manual-links`. This path does **not** run job discovery or the production eligibility filters. It resolves the supplied job/JD only, then hands that JD to the exact same `app.batch_prepare.prepare` resume pipeline used by Job Discovery.

Manual-link jobs have their own persistent queue under `JOB_AGENT_STATE_DIR/manual_job_links/`, including their resume artifacts. Dashboard sync from automated discovery does not replace this directory. Failed JD reads can be edited and retried, and successful rows can be opened, viewed as PDF, marked applied, or individually deleted.

The shared resume rules remain centralized: partial/conservative JDs and low safe-target counts use the master resume policy; usable JDs with sufficient safe targets use the existing JD-tailored LLM path; all generated artifacts still go through the same ATS audit, layout checks, DOCX→PDF conversion, and parity validation.



## Third dashboard path: Paste Job Description

The Railway dashboard has three independent entry paths:

1. **Job Discovery** (`/`): automated source discovery, job verification and production resume preparation.
2. **Manual Job Links** (`/manual-links`): paste an ATS/employer job URL, resolve the job and run the shared production resume pipeline.
3. **Paste Job Description** (`/pasted-jds`): paste the complete JD directly without requiring an external URL.

The third path calls **the same `batch_prepare.prepare()` resume-generation code** through `manual_job_links.run_shared_resume_pipeline()`: master-resume choice, JD coverage plan, Fidelity cloud selection, historical employer locks, AI-era restrictions, ATS and evidence checks, retries, master Word formatting, two-page layout verification, and DOCX/PDF parity. No separate prompt or independent resume generator is used.

**Required Railway variable:** set `PASTED_JD_ACCESS_TOKEN` on the **job-dashboard** service in the production environment to a long random secret. When browsing `/pasted-jds`, enter username `admin` and this token in the browser's HTTP Basic authorization prompt. This authentication protects pasted JDs, API actions, paid LLM calls, and generated resume downloads; the route is unavailable until the variable is set. Do not put the token in GitHub or source code and do not share it in chat.

**Other requirements:** maintain `OPENAI_API_KEY` and the resume LLM model/settings on Railway `job-dashboard`, plus the configured Word/PDF conversion dependencies and the persistent volume at `/data`.

The third path stores its independent history under `${JOB_AGENT_STATE_DIR}/pasted_job_descriptions`. Submissions are de-duplicated by exact description + title + company; past submissions have date/status filters, retry/delete actions, and downloadable validated Word/PDF artifacts. Failed generations remain visible with their error. A paste is **not** employer ATS verification, an official posting date, or eligibility confirmation. A successful document is marked **Resume Ready** rather than pretending a job is verified as Ready to Apply. An optional application URL is required before the dashboard can record a manual application as applied. The first two paths and their state remain unchanged.


## Five directly actionable job-board sources

The automated **Job Discovery** flow runs all current employer/ATS integrations plus the user-approved job-board sources **HiringCafe**, **Welcome to the Jungle**, **Himalayas**, **Y Combinator Jobs**, and **Wellfound**. Dice is disabled on request; other ATS and portal connections are unchanged.

A job from one of these five boards may use `application_route=JOB_BOARD_MANUAL` instead of the employer-ATS-required route, provided the board supplies a specific job URL, role details, admissible source posting/update date in the production window, and positive live-source evidence. The normal U.S. work authorization, title, years of experience, job type, citizenship/clearance, prior-employer, duplicate, and DOCX/PDF validation gates still apply. The user opens the original source listing and manually submits their application; the system **never claims automatic submission**.

**Verification distinction:** `posting_verification_scope=JOB_BOARD_ONLY` is not employer/ATS validation. `freshness_basis=job_board_posting_date` labels a board posting date; `job_board_last_updated` labels Himalayas' documented `pubDate` field (last update, not necessarily original posting). The dashboard labels those dates explicitly. Missing/old date, invalid/non-job URL, or known closed listing remains on hold; no date or availability is fabricated. For browser-login-protected board links, a current confirmed source feed can support **manual** review while exposing `live_check.browser_access_unverified=true`.

**Himalayas:** The provider's API explicitly requires the original `applicationLink` for every Apply button and visible attribution. The agent retains that URL unmodified, labels Himalayas as the source, and checks the official single-job API for 200/open, 404/not found, or 410/closed before preparing a resume; browser-page scraping is not required. Its feed is refreshed daily and should not be aggressively polled.

**Other job boards:** Native job-detail URLs are retained for manually applying through HiringCafe, Welcome to the Jungle, Y Combinator, and Wellfound. Public source content is verified as far as the provider allows. The other aggregator/board providers retain the original employer/ATS verification policy.

The focused regression suite is `tests/test_approved_board_application_route.py` and the bounded live production-path check is `.github/workflows/five-board-manual-apply-e2e.yml`. The latter tests with an isolated ledger and at most one paid resume, never publishes test jobs to the production Railway application history.
