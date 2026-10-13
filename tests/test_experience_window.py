from app.eligibility import experience_check


PROFILE = {
    "candidate_experience_years": 5,
    "preferences": {"min_required_years": 4, "max_required_years": 6},
}


def _job(years: int) -> dict:
    return {
        "title": "Senior Data Engineer",
        "description": f"Requires {years}+ years of professional experience in data engineering.",
    }


def test_experience_window_accepts_four_five_and_six():
    for years in (4, 5, 6):
        result = experience_check(_job(years), PROFILE)
        assert result["eligible"] is True, years
        assert result["category"] == "EXPERIENCE_ELIGIBLE", years
        assert result["configured_window"] == [4, 6]


def test_experience_window_accepts_lower_minimum_when_candidate_exceeds_it():
    for years in (1, 2, 3):
        result = experience_check(_job(years), PROFILE)
        assert result["eligible"] is True, years
        assert result["category"] == "EXPERIENCE_BELOW_PREFERRED_MINIMUM", years
        assert result["configured_window"] == [4, 6]


def test_experience_window_rejects_above_six():
    for years in (7, 8):
        result = experience_check(_job(years), PROFILE)
        assert result["eligible"] is False, years
        assert result["category"] == "EXPERIENCE_TOO_SENIOR", years
