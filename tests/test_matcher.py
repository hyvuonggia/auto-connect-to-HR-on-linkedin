"""Tests for the keyword matching policy."""

from __future__ import annotations

import pytest

from linkedin_auto_connect.config import DEFAULT_KEYWORDS
from linkedin_auto_connect.matcher import KeywordMatcher

# One realistic card text per default keyword, proving parity with the old
# single-file ``matches_keywords()`` for every configured keyword.
PARITY_TEXTS = {
    "hr": "HR Business Partner at Northwind",
    "recruiter": "Senior Technical Recruiter at Contoso",
    "talent": "Talent Sourcer, Engineering",
    "acquisition": "Talent Acquisition Specialist",
    "human": "Human Resources Generalist",
    "resource": "Human Resource Coordinator",
    "recruitment": "Recruitment Consultant at Fabrikam",
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
    """A short keyword such as "an" must not match inside a longer word."""
    matcher = KeywordMatcher(["an"])
    assert matcher.matches("Leanne") is False
    assert matcher.matches("Anand") is False
    # A standalone token is a whole word, so it still matches. This mirrors the
    # old ``\ban\b`` logic; only substrings inside words are rejected.
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
    """A keyword such as "c++" is literal, never a regex quantifier."""
    matcher = KeywordMatcher(["c++"])
    assert matcher.keywords == ("c++",)
    assert matcher.matches("ccc") is False
    assert matcher.matches("c") is False
    # The mandated trailing \b sits after a non-word character, so it can only
    # match when a word character follows the literal "c++".
    assert matcher.matches("skills: c++dev") is True


def test_dot_in_keyword_is_literal() -> None:
    """A dot in a keyword does not act as a regex wildcard."""
    matcher = KeywordMatcher(["node.js"])
    assert matcher.matches("Backend engineer (Node.js)") is True
    assert matcher.matches("nodexjs") is False
