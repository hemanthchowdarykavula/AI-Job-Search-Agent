from app.filters import location_is_us


def test_workday_india_country_code_is_not_indiana():
    assert location_is_us("IN, Bangalore Kar", "workday", "") is False
    assert location_is_us("IN, Bengaluru Karnataka", "workday", "") is False


def test_real_indiana_location_still_qualifies():
    assert location_is_us("Indianapolis, IN", "workday", "") is True
    assert location_is_us("Indianapolis, Indiana", "workday", "") is True


def test_foreign_location_wins_before_state_abbreviation_without_us_role_evidence():
    assert location_is_us("IN, Bangalore Kar | Hyderabad, India", "workday", "") is False


def test_foreign_markers_require_token_boundaries():
    assert location_is_us("Indianapolis, IN", "workday", "") is True
    assert location_is_us("India", "workday", "") is False
    assert location_is_us("Bangalore, India", "workday", "") is False


def test_generic_remote_requires_us_scope_for_workday():
    assert location_is_us("Remote", "workday", "This role is based in the United States") is True
    assert location_is_us("Remote", "workday", "Remote role based in India") is False


def test_emea_label_is_not_overridden_by_generic_us_company_mention():
    description = "Our distributed team has employees in the USA, Canada, UK, and Switzerland."
    assert location_is_us("Remote - EMEA", "greenhouse", description) is False


def test_explicit_emea_label_stays_rejected_even_if_jd_mentions_us_scope():
    description = "This position is based in the United States and may be performed remotely from the U.S."
    assert location_is_us("Remote - EMEA", "greenhouse", description) is False


def test_explicit_emea_label_stays_rejected_even_with_us_application_question():
    question = "Are you currently located in the United States?"
    assert location_is_us("Remote - EMEA", "greenhouse", "", question) is False


def test_global_and_worldwide_roles_are_not_us_based():
    assert location_is_us("Remote - Global", "greenhouse", "We have offices in the USA") is False
    assert location_is_us("Worldwide", "lever", "Our company serves United States customers") is False
    assert location_is_us("Remote - Global", "greenhouse", "This role is based in the United States") is False


def test_missing_location_needs_explicit_us_vacancy_scope():
    assert location_is_us("", "greenhouse", "Our engineering team includes people in the USA and Europe") is False
    assert location_is_us("", "greenhouse", "This position is based in the United States") is True


def test_application_question_can_prove_us_scope_when_location_missing():
    question = "Are you currently located in the United States?"
    assert location_is_us("", "greenhouse", "", question) is True


def test_application_question_can_prove_us_scope_for_ambiguous_remote_label():
    question = "Do you currently reside in the United States?"
    assert location_is_us("Remote", "greenhouse", "", question) is True


def test_unknown_dice_location_is_not_assumed_us():
    assert location_is_us("", "dice", "Python SQL Spark data engineering") is False


def test_us_state_code_overrides_ambiguous_foreign_city_name():
    assert location_is_us("Dublin, CA; San Francisco, CA", "greenhouse", "") is True
    assert location_is_us("Vancouver, WA", "lever", "") is True


def test_us_city_can_be_embedded_in_structured_location_text():
    assert location_is_us("Chicago - 20 S. Wacker", "workday", "") is True


def test_missing_structured_location_can_use_explicit_us_title_location():
    assert location_is_us("", "dice", "", "", "Senior Data Engineer Location: Cary, NC (Onsite)") is True
    assert location_is_us("", "dice", "", "", "Data Engineer-Only W2-NY,NJ") is True


def test_title_without_us_location_does_not_bypass_location_requirement():
    assert location_is_us("", "dice", "", "", "Senior Data Engineer") is False


def test_title_state_abbreviation_must_be_location_shaped():
    assert location_is_us("", "dice", "", "", "MS Fabric Data Engineer") is False
    assert location_is_us("", "dice", "", "", "Senior Data Engineer - TX") is True
