"""Tests for the keyword matching policy.

``PARITY_TEXTS`` holds one realistic card text per default keyword, proving
parity with the old single-file ``matches_keywords()`` for every keyword.
"""

from __future__ import annotations

import pytest

from fakes import FakeClassifier, it_decision
from linkedin_auto_connect.config import DEFAULT_KEYWORDS
from linkedin_auto_connect.domain.matcher import (
    ItRelevanceMatcher,
    KeywordMatcher,
    candidate_name,
)
from linkedin_auto_connect.domain.models import JevRuntimeError, JevSetupError

PARITY_TEXTS = {
    "hr": "HR Business Partner at Northwind",
    "recruiter": "Senior Technical Recruiter at Contoso",
    "talent": "Talent Sourcer, Engineering",
    "acquisition": "Talent Acquisition Specialist",
    "human": "Human Resources Generalist",
    "resource": "Human Resource Coordinator",
    "recruitment": "Recruitment Consultant at Fabrikam",
    "recruiting": "Recruiting Intern at Contoso",
    "headhunter": "Independent Headhunter",
    "hiring": "Hiring Manager, Platform",
    "employment": "Employment Law Advisor",
    "people": "People Operations Lead",
    "onboarding": "Onboarding Specialist",
    "learning": "Learning and Development Manager",
    "looking for": "Currently looking for new opportunities",
    "hrbp": "HRBP, EMEA",
}


def test_parity_texts_cover_every_default_keyword() -> None:
    """The parity table must stay in sync with the default keyword list."""
    assert set(PARITY_TEXTS) == set(DEFAULT_KEYWORDS)


@pytest.mark.parametrize("keyword", DEFAULT_KEYWORDS)
def test_every_default_keyword_matches_a_typical_card(keyword: str) -> None:
    """Each shipped keyword is found in a realistic profile-card text."""
    assert KeywordMatcher(DEFAULT_KEYWORDS).matches(PARITY_TEXTS[keyword]) is True


def test_word_boundaries_do_not_match_inside_a_word() -> None:
    """A short keyword such as "an" must not match inside a longer word.

    A standalone token is still a whole word and does match, mirroring the old
    ``\ban\b`` logic: only substrings inside words are rejected.
    """
    matcher = KeywordMatcher(["an"])
    assert matcher.matches("Leanne") is False
    assert matcher.matches("Anand") is False
    assert matcher.matches("Le An") is True
    assert matcher.matches("Le An, an engineer") is True


def test_name_only_text_does_not_match_default_keywords() -> None:
    """A card text that is only a name contains no default HR keyword."""
    matcher = KeywordMatcher(DEFAULT_KEYWORDS)
    assert matcher.matches("Le An") is False
    assert matcher.matches("Le An, an engineer") is False


def test_keyword_does_not_match_as_a_substring() -> None:
    """A keyword must not match as a substring of a longer word."""
    matcher = KeywordMatcher(["hr"])
    assert matcher.matches("Worked through the backlog") is False
    assert matcher.matches("HR operations") is True


def test_multi_word_keyword_matches_as_a_phrase() -> None:
    """The phrase keyword "looking for" matches, including in another case."""
    matcher = KeywordMatcher(["looking for"])
    assert matcher.matches("Currently looking for new opportunities") is True
    assert matcher.matches("LOOKING FOR a role") is True
    assert matcher.matches("Looking  for a role") is False


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_text_never_matches(text: str | None) -> None:
    """``None``, the empty string and blank text match nothing."""
    assert KeywordMatcher(DEFAULT_KEYWORDS).matches(text) is False


def test_matching_is_case_insensitive() -> None:
    """Keywords and text are compared without regard to case."""
    matcher = KeywordMatcher(["HR"])
    assert matcher.matches("hr partner") is True
    assert matcher.matches("Hr Partner") is True
    assert matcher.matches("HR PARTNER") is True


def test_keywords_are_normalised_for_matching() -> None:
    """Keywords are stripped, lowercased, deduplicated and blanks dropped."""
    matcher = KeywordMatcher([" HR ", "hr", "Recruiter", "", "   "])
    assert matcher.keywords == ("hr", "recruiter")
    assert matcher.matches("Technical Recruiter") is True


def test_no_keywords_matches_nothing() -> None:
    """An empty keyword set never matches, even on keyword-heavy text."""
    assert KeywordMatcher([]).matches("HR Recruiter and Talent Acquisition") is False


def test_special_regex_characters_are_escaped() -> None:
    """A keyword such as "c++" is literal, never a regex quantifier.

    The trailing ``\\b`` sits after a non-word character, so it only matches when
    a word character follows the literal "c++".
    """
    matcher = KeywordMatcher(["c++"])
    assert matcher.keywords == ("c++",)
    assert matcher.matches("ccc") is False
    assert matcher.matches("c") is False
    assert matcher.matches("skills: c++dev") is True


def test_dot_in_keyword_is_literal() -> None:
    """A dot in a keyword does not act as a regex wildcard."""
    matcher = KeywordMatcher(["node.js"])
    assert matcher.matches("Backend engineer (Node.js)") is True
    assert matcher.matches("nodexjs") is False


def test_candidate_name_ignores_a_separate_degree_line() -> None:
    """A LinkedIn degree marker on its own line is not part of the name."""
    assert (
        candidate_name("Oanh Nguyen Hoang\n• 2nd\nIT Recruiter")
        == "Oanh Nguyen Hoang"
    )
    assert candidate_name("Anh Nguyen • 2nd\nIT Recruiter") == "Anh Nguyen"
    assert candidate_name("• 2nd\nAnh Nguyen") == "Anh Nguyen"
    assert candidate_name(None) == "?"


def test_it_matcher_does_not_call_jev_when_the_keyword_misses() -> None:
    """The keyword prefilter keeps Jev calls off non-candidates."""
    classifier = FakeClassifier([it_decision()])
    matcher = ItRelevanceMatcher(KeywordMatcher(["hr"]), classifier)

    assert matcher.matches("software engineer") is False
    assert classifier.texts == []


def test_it_matcher_connects_hr_people_in_it() -> None:
    """An HR keyword match plus a confident HR/IT decision is accepted."""
    classifier = FakeClassifier([it_decision()])
    matcher = ItRelevanceMatcher(KeywordMatcher(["recruiter"]), classifier)

    assert matcher.matches("IT Recruiter at Acme Software") is True
    assert classifier.texts == ["IT Recruiter at Acme Software"]


def test_it_matcher_rejects_a_non_hr_person_matched_by_a_broad_keyword() -> None:
    """A software engineer matching "looking for" is not an HR candidate."""
    classifier = FakeClassifier([it_decision(is_hr=0.02)])
    matcher = ItRelevanceMatcher(KeywordMatcher(["looking for"]), classifier)

    assert matcher.matches("Software engineer looking for new opportunities") is False


def test_it_matcher_rejects_hr_at_a_non_it_company() -> None:
    """HR work with no IT connection is skipped even when the person is HR."""
    classifier = FakeClassifier([it_decision(it_related=0.05)])
    matcher = ItRelevanceMatcher(KeywordMatcher(["hr"]), classifier)

    assert matcher.matches("HR Business Partner at a retailer") is False


def test_it_matcher_skips_an_uncertain_it_answer() -> None:
    """A probability between the yes and no thresholds is not acted on."""
    classifier = FakeClassifier([it_decision(it_related=0.5)])
    matcher = ItRelevanceMatcher(KeywordMatcher(["hr"]), classifier)

    assert matcher.matches("HR Business Partner at Acme") is False


def test_it_matcher_requires_enough_evidence() -> None:
    """A sparse card is skipped even when the other answers look strong."""
    classifier = FakeClassifier([it_decision(evidence=0.2)])
    matcher = ItRelevanceMatcher(KeywordMatcher(["recruiter"]), classifier)

    assert matcher.matches("Recruiter") is False


def test_it_matcher_thresholds_are_inclusive() -> None:
    """Exactly at the thresholds is accepted; just below evidence is not."""
    matcher = ItRelevanceMatcher(
        KeywordMatcher(["hr"]),
        FakeClassifier(
            [
                it_decision(is_hr=0.7, it_related=0.7, evidence=0.4),
                it_decision(is_hr=0.7, it_related=0.7, evidence=0.39),
            ]
        ),
    )

    assert matcher.matches("HR manager") is True
    assert matcher.matches("HR manager") is False


def test_it_matcher_falls_back_to_the_keyword_result_on_setup_error() -> None:
    """A bad key or missing credit restores the old HR-only behaviour once."""
    classifier = FakeClassifier([JevSetupError("bad key")])
    matcher = ItRelevanceMatcher(KeywordMatcher(["hr"]), classifier)

    assert matcher.matches("HR manager") is True
    assert matcher.matches("HR manager") is True
    assert len(classifier.texts) == 1
    assert matcher.jev_active is False


def test_it_matcher_skips_after_runtime_failures_and_stops_calling() -> None:
    """Transient failures skip cards, then disable Jev after the budget."""
    classifier = FakeClassifier([JevRuntimeError("timeout")], default=JevRuntimeError("timeout"))
    matcher = ItRelevanceMatcher(
        KeywordMatcher(["hr"]),
        classifier,
        max_failures=3,
    )

    assert [matcher.matches("HR manager") for _ in range(3)] == [False, False, False]
    assert len(classifier.texts) == 3
    assert matcher.matches("HR manager") is False
    assert len(classifier.texts) == 3
    assert matcher.jev_active is False
    assert matcher.unavailable is True
