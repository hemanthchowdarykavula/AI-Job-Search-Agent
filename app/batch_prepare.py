from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from app.ats_audit import ats_audit
from app.config import load_profile
from app.jd_coverage_plan import build_coverage_plan
from app.llm_resume_writer import generate_with_llm
from app.master_resume import experience_bullet_counts, master_resume_payload
from app.pdf_export import convert_docx_to_pdf_detailed, validate_docx_pdf_parity
from app.reference_resume_formatter import render_llm_resume
from app.resume_tailoring_policy import determine_tailoring_policy

load_dotenv()

MAX_RESUME_ATTEMPTS = 2
DRAFT_RESUME_DIR = "generated/.resume_drafts"
FINAL_RESUME_DIR = "generated/resumes"

# These checks protect factual/structural correctness. Everything else produced by
# ats_audit (ATS score, summary density, experience-depth %, readability, repetition,
# etc.) is useful diagnostic information but must never discard an otherwise valid
# resume.
BLOCKING_AUDIT_GATES = (
    "structure",
    "metrics",
    "domain_coherence",
    "cloud_credibility",
)


def _bullet_contract_text() -> str:
    counts = experience_bullet_counts()
    return (
        f'{counts.get("Fidelity Investments", 0)} Fidelity, '
        f'{counts.get("Cigna Healthcare", 0)} Cigna, and '
        f'{counts.get("Target Corporation", 0)} Target bullets'
    )


def _base_resume_payload(profile=None):
    """Return the user-uploaded master resume exactly; no JD tailoring and no LLM."""
    return master_resume_payload()


def _discard_resume_artifact(resume_path):
    if not resume_path:
        return
    path = Path(resume_path)
    parent = path.parent
    try:
        if parent.exists() and parent.is_dir():
            shutil.rmtree(parent)
        elif path.exists():
            path.unlink()
    except Exception as exc:
        print(f"WARNING: failed to remove rejected resume artifact {path}: {exc}", flush=True)


def _render_draft(job, profile, payload):
    """Render and hard-validate layout before a draft can enter ATS review.

    A screenshot-style artifact with a third page, sparse trailing page, displaced
    employer/education section, or legacy Environment footer is rejected here while
    it is still temporary. JD-tailored resumes can then be regenerated within the
    normal three-attempt loop instead of being promoted to production.
    """
    resume = render_llm_resume(job, profile, payload, output_dir=DRAFT_RESUME_DIR)
    conversion = convert_docx_to_pdf_detailed(resume, attempts=2)
    draft_pdf = conversion.get("pdf_path")
    validation = validate_docx_pdf_parity(resume, draft_pdf)
    validation["attempts"] = conversion.get("attempts", 0)
    validation["conversion_reason"] = conversion.get("reason")
    validation["renderer"] = conversion.get("renderer")

    if not validation.get("passed"):
        reason = validation.get("reason") or "unknown layout validation failure"
        _discard_resume_artifact(resume)
        raise RuntimeError(f"Resume layout contract failed before promotion: {reason}")

    # The preflight PDF is disposable. Production creates a fresh PDF only after
    # the DOCX also clears the content audit and is promoted to the final directory.
    if draft_pdf:
        try:
            Path(draft_pdf).unlink(missing_ok=True)
        except Exception:
            pass
    return resume


def _promote_approved_resume(draft_path):
    draft = Path(draft_path)
    final_root = Path(__file__).resolve().parents[1] / FINAL_RESUME_DIR
    final_dir = final_root / draft.parent.name
    final_dir.mkdir(parents=True, exist_ok=False)
    final_path = final_dir / draft.name
    shutil.move(str(draft), str(final_path))
    try:
        draft.parent.rmdir()
    except OSError:
        pass
    return str(final_path)


def _render_base_resume(job, profile):
    return _render_draft(job, profile, _base_resume_payload(profile))


def _retryable_resume_error(exc):
    text = str(exc).lower()
    transient_markers = (
        "credit_balance_exhausted",
        "insufficient_quota",
        "rate limit",
        "429",
        "temporarily unavailable",
        "timeout",
        "timed out",
        "connection error",
        "connection reset",
        "service unavailable",
        "502",
        "503",
        "504",
    )
    return any(marker in text for marker in transient_markers)


def _should_use_master_resume(raw, coverage_plan):
    """Choose master vs JD-tailored content.

    Normal discovery keeps the pre-formatting policy. A manually supplied link
    may opt into JD tailoring only after the manual resolver has confirmed that
    a usable job description exists; all downstream generation/audit rules stay
    identical to production.
    """
    if raw.get("force_jd_tailoring") is True:
        return False
    targets = int(coverage_plan.get("target_count") or 0)
    if targets <= 2:
        return True
    if raw.get("tailoring_mode") == "BASE_RESUME_CONSERVATIVE":
        return True
    if raw.get("description_complete") is False:
        return True
    return False


def _blocking_audit_failures(audit):
    gates = audit.get("quality_gates", {}) or {}
    return [gate for gate in BLOCKING_AUDIT_GATES if gate in gates and not gates[gate]]


def _make_quality_heuristics_advisory(audit):
    """Keep scoring heuristics advisory while layout remains a separate hard gate."""
    strict_passed = bool(audit.get("passed"))
    gates = dict(audit.get("quality_gates", {}) or {})
    blocking_failures = _blocking_audit_failures(audit)
    advisory_failures = [
        gate for gate, value in gates.items()
        if not value and gate not in BLOCKING_AUDIT_GATES
    ]

    audit["strict_audit_passed"] = strict_passed
    audit["blocking_quality_gates"] = blocking_failures
    audit["advisory_quality_gates"] = advisory_failures
    audit["quality_heuristics_are_advisory"] = True
    audit["page_count_is_advisory"] = False
    audit["passed"] = not blocking_failures
    audit["quality_gate_passed"] = not blocking_failures
    audit["status"] = "ATS_PASS" if audit["passed"] else "HOLD_CONTENT_CORRECTNESS"
    audit["decision_note"] = (
        "Internal ATS score, summary/skills density, experience-depth score, readability, "
        "repetition, and master-retention score are advisory. Structural/factual correctness "
        "and the separate two-page master-like PDF layout contract are hard production gates."
    )
    return audit


def _critical_audit_feedback(audit):
    return {
        "blocking_quality_gates": audit.get("blocking_quality_gates", []),
        "bullet_counts": audit.get("bullet_counts", {}),
        "metric_violations": audit.get("metric_violations", {}),
        "unapproved_metric_claims": audit.get("unapproved_metric_claims", []),
        "domain_coherence_violations": audit.get("domain_coherence_violations", []),
        "cloud_policy_violations": audit.get("cloud_policy_violations", []),
        "retry_instruction": (
            "Correct only the blocking factual/structural issues. Preserve the JD-tailored "
            f"content that is already valid. Keep exactly {_bullet_contract_text()}; "
            "preserve employer domains and cloud rules; do not invent numerical "
            "claims. Keep wording concise enough to preserve the master-like two-page layout."
        ),
    }


def _render_error_feedback(exc):
    return {
        "render_error": str(exc),
        "retry_instruction": (
            "Correct the structural or layout error. Return one substantial 100-140 word summary paragraph, "
            f"compact Technical Skills categories, exactly {_bullet_contract_text()}, "
            "plus a concise skills_used technology list for each employer. "
            "Do not return Environment paragraphs. Keep each bullet to one concise engineering "
            "sentence and preserve the two-page master-like layout without sparse trailing pages."
        ),
    }


def _matches(raw, company=None, title=None, external_id=None):
    if external_id and raw.get("external_id") != external_id:
        return False
    if company and company.lower() not in (raw.get("company_key") or raw.get("company") or "").lower():
        return False
    if title and title.lower() not in (raw.get("title") or "").lower():
        return False
    return True


def prepare(
    report_path,
    output_path="generated/application_manifest.json",
    debug_company=None,
    debug_title=None,
    external_id=None,
    limit=None,
):
    """Prepare a resume for every FINAL_JD_VERIFIED job; never submit applications."""
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    profile = load_profile()
    manifest = []
    matched = 0
    priority = {
        ("FULL_JD", "EXTERNAL_ATS"): 0,
        ("FULL_JD", "DICE"): 1,
        ("BASE_RESUME_CONSERVATIVE", "EXTERNAL_ATS"): 2,
        ("BASE_RESUME_CONSERVATIVE", "DICE"): 3,
    }
    results = sorted(
        report.get("results", []),
        key=lambda item: priority.get(
            (item.get("job", {}).get("tailoring_mode"), item.get("job", {}).get("application_route")),
            9,
        ),
    )

    for item in results:
        if item.get("action") != "FINAL_JD_VERIFIED":
            continue
        raw = item["job"]
        if not _matches(raw, debug_company, debug_title, external_id):
            continue
        if limit is not None and matched >= limit:
            break
        matched += 1

        elig = item["eligibility"]
        company = raw.get("company_key") or raw.get("company") or "Unknown"
        job = SimpleNamespace(
            company=company,
            title=raw.get("title") or "",
            description=raw.get("description") or "",
            location=raw.get("location"),
            employment_type=raw.get("employment_type"),
            url=raw.get("url"),
            description_complete=raw.get("description_complete"),
            description_usable=raw.get("description_usable"),
            tailoring_mode=raw.get("tailoring_mode"),
        )
        print(f"START {job.company} | {job.title} | FINAL_JD_VERIFIED", flush=True)

        audit_history = []
        resume = None
        pdf_path = None
        tailoring_policy = None
        attempts = 0

        try:
            if not (
                raw.get("description_complete")
                or raw.get("description_usable")
                or raw.get("tailoring_mode") == "BASE_RESUME_CONSERVATIVE"
            ):
                raise RuntimeError(
                    "Job description is not usable for safe resume preparation; run app.jd_finalizer first."
                )

            coverage_plan = build_coverage_plan(job, profile)
            tailoring_policy = determine_tailoring_policy(job, coverage_plan)
            use_master_resume = _should_use_master_resume(raw, coverage_plan)
            print(
                "Resume policy | targets={} | mode={} | use_master={}".format(
                    coverage_plan.get("target_count"),
                    tailoring_policy.get("mode"),
                    use_master_resume,
                ),
                flush=True,
            )

            if use_master_resume:
                resume = _render_base_resume(job, profile)
                audit = {
                    "passed": True,
                    "strict_audit_passed": None,
                    "quality_heuristics_are_advisory": True,
                    "generation_source": "uploaded_master_conservative_fallback",
                    "generation_attempts": 0,
                    "target_count": int(coverage_plan.get("target_count") or 0),
                    "quality_gates": {},
                    "blocking_quality_gates": [],
                    "advisory_quality_gates": [],
                    "decision_note": (
                        "Master resume used unchanged because the JD is partial/conservative "
                        "or contains at most two safe tailoring targets."
                    ),
                }
                audit_history = [{"version": "MASTER", "resume_path": str(resume), "audit": audit}]
            else:
                feedback = None
                last_error = None
                while attempts < MAX_RESUME_ATTEMPTS:
                    attempts += 1
                    print(f"Generating JD-tailored resume V{attempts}/{MAX_RESUME_ATTEMPTS}...", flush=True)
                    generated = generate_with_llm(
                        job,
                        profile,
                        feedback,
                        coverage_plan=coverage_plan,
                    )
                    if not generated:
                        raise RuntimeError(
                            "LLM resume generation is unavailable. Check OPENAI_API_KEY and RESUME_LLM_MODEL in .env."
                        )

                    try:
                        resume = _render_draft(job, profile, generated)
                    except Exception as exc:
                        last_error = exc
                        print(f"V{attempts} render/layout error | {exc}", flush=True)
                        if attempts >= MAX_RESUME_ATTEMPTS:
                            raise
                        feedback = _render_error_feedback(exc)
                        continue

                    audit = _make_quality_heuristics_advisory(ats_audit(job, profile, resume))
                    audit_history.append({"version": attempts, "resume_path": str(resume), "audit": audit})
                    print(
                        f"V{attempts} audit | production_pass={audit['passed']} | "
                        f"strict_score_pass={audit.get('strict_audit_passed')} | "
                        f"ATS={audit.get('internal_ats_score')} | "
                        f"advisory={audit.get('advisory_quality_gates')} | "
                        f"blocking={audit.get('blocking_quality_gates')}",
                        flush=True,
                    )
                    if audit["passed"]:
                        break

                    if attempts < MAX_RESUME_ATTEMPTS:
                        feedback = _critical_audit_feedback(audit)
                        _discard_resume_artifact(resume)
                        audit_history[-1]["resume_path"] = None
                        resume = None

                if not resume:
                    if last_error:
                        raise last_error
                    raise RuntimeError("Resume generation did not produce a renderable artifact")

                audit["generation_attempts"] = attempts
                audit["generation_source"] = "openai_jd_tailored_format_preserving"

            artifact_validation = {"passed": False, "reason": "PDF conversion not attempted", "attempts": 0}

            if audit["passed"]:
                resume = _promote_approved_resume(resume)
                if audit_history:
                    audit_history[-1]["resume_path"] = str(resume)

                conversion = convert_docx_to_pdf_detailed(resume, attempts=2)
                pdf_path = conversion["pdf_path"]
                artifact_validation = validate_docx_pdf_parity(resume, pdf_path)
                artifact_validation["attempts"] = conversion["attempts"]
                artifact_validation["conversion_reason"] = conversion["reason"]
                artifact_validation["renderer"] = conversion["renderer"]

            if not audit["passed"]:
                _discard_resume_artifact(resume)
                for history in audit_history:
                    _discard_resume_artifact(history.get("resume_path"))
                    history["resume_path"] = None
                resume = None
                pdf_path = None
                next_action = "HOLD_ATS_REVIEW"
            elif not artifact_validation["passed"]:
                # The final conversion must independently re-pass the same hard
                # two-page master-like layout contract used during draft preflight.
                _discard_resume_artifact(resume)
                resume = None
                pdf_path = None
                next_action = "HOLD_ARTIFACT_VALIDATION"
            else:
                next_action = "READY_TO_APPLY"

            print(
                f"DONE {job.company} | next_action={next_action} | attempts={attempts} | "
                f"ATS={audit.get('internal_ats_score')} | pages={artifact_validation.get('page_count')}",
                flush=True,
            )

        except Exception as exc:
            try:
                _discard_resume_artifact(resume)
            except Exception:
                pass
            for history in audit_history:
                _discard_resume_artifact(history.get("resume_path"))
                history["resume_path"] = None
            retryable = _retryable_resume_error(exc)
            print(f"RESUME PIPELINE ERROR: {exc}", flush=True)
            resume = None
            pdf_path = None
            next_action = "RETRY_RESUME_GENERATION" if retryable else "HOLD_RESUME_ERROR"
            audit = {
                "passed": False,
                "generation_source": "resume_pipeline_error",
                "error": str(exc),
                "generation_attempts": attempts,
                "retryable": retryable,
                "resume_tailoring_policy": tailoring_policy,
            }
            artifact_validation = {"passed": False, "reason": str(exc)}

        manifest.append(
            {
                "external_id": raw.get("external_id"),
                "source": raw.get("source"),
                "company": job.company,
                "title": job.title,
                "url": job.url,
                "original_url": raw.get("original_url"),
                "source_apply_url": raw.get("source_apply_url"),
                "source_credit": raw.get("source_credit"),
                "ats_provider": raw.get("ats_provider"),
                "ats_identifier": raw.get("ats_identifier"),
                "ats_resolution": raw.get("ats_resolution"),
                "application_route": raw.get("application_route"),
                "requisition_id": raw.get("requisition_id") or raw.get("job_id"),
                "freshness_proof": raw.get("freshness_proof"),
                "official_posted_at": raw.get("official_posted_at") or raw.get("freshness_verified_posted_at"),
                "official_posted_label": raw.get("official_posted_label"),
                "freshness_basis": raw.get("freshness_basis"),
                "recovery_scan": bool(raw.get("recovery_scan")),
                "discovery_window_hours": raw.get("discovery_window_hours"),
                "live_check": raw.get("live_check"),
                "application_questions": raw.get("application_questions"),
                "screening_questions": raw.get("screening_questions"),
                "questions": raw.get("questions"),
                "application_form": raw.get("application_form"),
                "tailoring_mode": raw.get("tailoring_mode"),
                "description": raw.get("description"),
                "description_complete": raw.get("description_complete"),
                "description_usable": raw.get("description_usable"),
                "employment_type": raw.get("employment_type"),
                "location": raw.get("location"),
                "eligibility": elig,
                "experience": elig["experience"],
                "sponsorship": elig["sponsorship"],
                "resume_path": resume,
                "pdf_path": pdf_path,
                "resume_tailoring_policy": tailoring_policy,
                "ats_audit": audit,
                "artifact_validation": artifact_validation,
                "audit_history": audit_history,
                "next_action": next_action,
                "application_status": "NOT_STARTED",
            }
        )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="generated/eligible_jobs.json")
    parser.add_argument("--output", default="generated/application_manifest.json")
    parser.add_argument("--debug-company")
    parser.add_argument("--debug-title")
    parser.add_argument("--external-id")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    rows = prepare(
        args.report,
        args.output,
        args.debug_company,
        args.debug_title,
        args.external_id,
        args.limit,
    )
    actions = (
        "READY_TO_APPLY",
        "HOLD_ATS_REVIEW",
        "HOLD_ARTIFACT_VALIDATION",
        "HOLD_RESUME_ERROR",
        "RETRY_RESUME_GENERATION",
    )
    counts = {action: sum(row["next_action"] == action for row in rows) for action in actions}
    print(json.dumps({"prepared": len(rows), **counts}, indent=2))
    print(f"Saved manifest to {args.output}")
