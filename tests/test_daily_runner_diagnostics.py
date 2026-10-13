from app.daily_runner import _reason_key


def test_job_family_reason_is_not_misclassified_as_other():
    assert _reason_key("title/JD outside data-engineering job family") == "wrong_job_family"


def test_hard_filter_reasons_are_split_into_actionable_buckets():
    assert _reason_key("location outside United States target") == "location_outside_us"
    assert _reason_key("employment type outside Full-Time/W-2 target") == "employment_type_mismatch"
    assert _reason_key("US citizenship required") == "citizenship_required"
    assert _reason_key("security/public-trust clearance required") == "clearance_required"
    assert _reason_key("excluded prior employer") == "excluded_employer"
