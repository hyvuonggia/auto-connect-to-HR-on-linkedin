"""Tests for the argparse CLI in :mod:`linkedin_auto_connect.cli`."""

from __future__ import annotations

from pathlib import Path

import pytest

from linkedin_auto_connect import __version__
from linkedin_auto_connect.cli import _parse_keywords, build_parser, build_settings, main
from linkedin_auto_connect.config import Settings


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` prints usage and exits successfully."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--help"])

    assert excinfo.value.code == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    """``--version`` prints the version from the package."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])

    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_build_settings_without_flags_keeps_defaults() -> None:
    """No flags means the frozen defaults are used unchanged."""
    args = build_parser().parse_args([])

    assert build_settings(args) == Settings()


def test_build_settings_applies_every_flag() -> None:
    """Every settings-affecting flag replaces its default."""
    args = build_parser().parse_args(
        [
            "--max-invites",
            "42",
            "--no-headless",
            "--keywords",
            " recruiter, talent ,, ",
            "--cookies",
            "cookies.json",
            "--max-restarts",
            "3",
            "--scroll-pause",
            "0.5",
            "--click-pause",
            "0.25",
            "--verbose",
        ]
    )

    settings = build_settings(args)

    assert settings.max_invites_per_run == 42
    assert settings.headless is False
    assert settings.keywords == ("recruiter", "talent")
    assert settings.cookies_path == Path("cookies.json")
    assert settings.max_restarts == 3
    assert settings.scroll_pause_seconds == 0.5
    assert settings.click_pause_seconds == 0.25
    assert args.verbose is True


def test_headless_flag_can_turn_headless_back_on() -> None:
    """The BooleanOptionalAction pair supports both directions."""
    args = build_parser().parse_args(["--headless"])

    assert build_settings(args).headless is True


def test_build_settings_applies_jev_flags() -> None:
    """Every Jev/filter flag replaces its default."""
    args = build_parser().parse_args(
        [
            "--no-jev",
            "--jev-timeout",
            "9.5",
            "--it-yes-threshold",
            "0.8",
            "--it-no-threshold",
            "0.2",
            "--it-evidence-threshold",
            "0.5",
            "--it-cache",
            "decisions.json",
        ]
    )

    settings = build_settings(args)

    assert settings.jev_enabled is False
    assert settings.jev_timeout_seconds == 9.5
    assert settings.it_yes_threshold == 0.8
    assert settings.it_no_threshold == 0.2
    assert settings.it_evidence_threshold == 0.5
    assert settings.it_cache_path == Path("decisions.json")


@pytest.mark.parametrize(
    "flag",
    ["--it-yes-threshold", "--it-no-threshold", "--it-evidence-threshold"],
)
def test_probability_flags_reject_values_outside_zero_to_one(flag: str) -> None:
    """A threshold above 1 is a typo, not a silent no-op."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args([flag, "1.5"])

    assert excinfo.value.code == 2


def test_parse_keywords_trims_and_drops_blanks() -> None:
    """Comma-separated keywords are stripped, with empty entries removed."""
    assert _parse_keywords("  hr , recruiter ,, talent  ") == ("hr", "recruiter", "talent")
    assert _parse_keywords(" , ") == ()
    assert _parse_keywords(None) is None


def test_main_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``main(['--help'])`` returns 0 instead of leaking SystemExit."""
    assert main(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_main_returns_zero_even_when_invites_were_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invite count is a metric, not a process exit status."""
    monkeypatch.setattr(
        "linkedin_auto_connect.cli._run_application",
        lambda settings, interruption: 7,
    )

    assert main([]) == 0


@pytest.mark.parametrize("flag", ["--max-invites", "--max-restarts"])
def test_negative_counts_are_rejected(flag: str) -> None:
    """A negative cap or restart budget is a typo, not 'unlimited'."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args([flag, "-1"])

    assert excinfo.value.code == 2
