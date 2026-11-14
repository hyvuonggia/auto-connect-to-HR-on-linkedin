"""Tests for the Jev adapter, its env config, and the decision cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakes import FakeClassifier, it_decision
from linkedin_auto_connect.jev import (
    DEFAULT_BASE_URL,
    JevClassifier,
    JevConfig,
    load_env_file,
    load_jev_config,
    systemone_endpoint,
)
from linkedin_auto_connect.jev_cache import (
    CachedClassifier,
    JsonDecisionCache,
    decision_key,
)
from linkedin_auto_connect.models import JevRuntimeError, JevSetupError, RelevanceDecision

VALID_RESPONSE = {
    "model": "jev-test",
    "answers": {
        "is_hr_role": {"type": "noul", "noul": 0.96},
        "it_role_or_company": {"type": "noul", "noul": 0.91},
        "has_enough_evidence": {"type": "noul", "noul": 0.88},
    },
    "usage": {"input_tokens": 120, "output_tokens": 3, "cost": 0},
}


def make_config(**overrides: object) -> JevConfig:
    """Build a test config without reading the real environment."""
    values: dict[str, object] = {
        "api_key": "test-key",
        "base_url": "https://example.test/systemone",
        "model": "jev-test",
        "timeout_seconds": 5.0,
        "max_attempts": 3,
        "retry_backoff_seconds": 0.5,
    }
    values.update(overrides)
    return JevConfig(**values)  # type: ignore[arg-type]


def test_load_env_file_parses_quotes_comments_and_bom(tmp_path: Path) -> None:
    """A BOM, comments, blank lines and quoted values are all harmless."""
    env = tmp_path / ".env"
    env.write_text(
        '\ufeff# local secrets\nJEV_API_KEY="abc"\nJEV_BASE_URL=\'https://x.test\'\n',
        encoding="utf-8",
    )

    values = load_env_file(env)

    assert values == {"JEV_API_KEY": "abc", "JEV_BASE_URL": "https://x.test"}


def test_load_jev_config_requires_both_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial .env is not a usable configuration."""
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEV_BASE_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("JEV_API_KEY=only-key\n", encoding="utf-8")

    assert load_jev_config(env_file=env) is None

    env.write_text(
        "JEV_API_KEY=key\nJEV_BASE_URL=https://base.test\n", encoding="utf-8"
    )
    config = load_jev_config(env_file=env)

    assert config is not None
    assert config.api_key == "key"
    assert config.base_url == "https://base.test"


def test_environment_wins_over_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI can override the file without editing it."""
    env = tmp_path / ".env"
    env.write_text(
        "JEV_API_KEY=file-key\nJEV_BASE_URL=https://file.test\n", encoding="utf-8"
    )
    monkeypatch.setenv("JEV_API_KEY", "env-key")
    monkeypatch.setenv("JEV_BASE_URL", "https://env.test")

    config = load_jev_config(env_file=env)

    assert config is not None
    assert config.api_key == "env-key"
    assert config.base_url == "https://env.test"


def test_load_jev_config_requires_a_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key without a base URL is incomplete; the documented default is explicit."""
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEV_BASE_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("JEV_API_KEY=key\nJEV_BASE_URL=\n", encoding="utf-8")

    assert load_jev_config(env_file=env) is None
    assert DEFAULT_BASE_URL == "https://opencode.ai/inference/systemone"


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (
            "https://opencode.ai/inference/systemone",
            "https://opencode.ai/inference/systemone/v1/systemone",
        ),
        (
            "https://opencode.ai/inference/systemone/",
            "https://opencode.ai/inference/systemone/v1/systemone",
        ),
        (
            "https://opencode.ai/inference/systemone/v1/systemone",
            "https://opencode.ai/inference/systemone/v1/systemone",
        ),
    ],
)
def test_systemone_endpoint_accepts_a_base_or_a_full_url(base: str, expected: str) -> None:
    """Both the documented base and the full endpoint are accepted."""
    assert systemone_endpoint(base) == expected


def test_classify_parses_the_three_nouls_and_builds_the_request() -> None:
    """The adapter sends the card and returns all three probabilities."""
    calls: list[tuple[str, dict[str, str], dict[str, object], float]] = []

    def poster(
        url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> dict[str, object]:
        calls.append((url, dict(headers), json.loads(body.decode("utf-8")), timeout))
        return VALID_RESPONSE

    classifier = JevClassifier(make_config(), poster=poster)
    decision = classifier.classify("IT Recruiter\n\nAcme Software\n\nConnect")

    assert decision == RelevanceDecision(
        is_hr=0.96, it_related=0.91, evidence=0.88, model="jev-test"
    )
    url, headers, payload, timeout = calls[0]
    assert url == "https://example.test/systemone/v1/systemone"
    assert headers["Authorization"] == "Bearer test-key"
    assert timeout == 5.0
    assert payload["model"] == "jev-test"
    assert payload["state"]["candidate"]["name"] == "IT Recruiter"
    assert "Acme Software" in payload["state"]["candidate"]["card_text"]
    assert set(payload["questions"]) == {
        "is_hr_role",
        "it_role_or_company",
        "has_enough_evidence",
    }


def test_classify_retries_transient_failures() -> None:
    """Runtime errors are retried with a growing backoff."""
    attempts: list[int] = []
    sleeps: list[float] = []

    def poster(*_args: object) -> dict[str, object]:
        attempts.append(1)
        if len(attempts) < 3:
            raise JevRuntimeError("timeout")
        return VALID_RESPONSE

    classifier = JevClassifier(
        make_config(max_attempts=3),
        poster=poster,  # type: ignore[arg-type]
        sleeper=sleeps.append,
    )

    decision = classifier.classify("IT Recruiter")

    assert decision.it_related == 0.91
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]


def test_classify_does_not_retry_a_setup_error() -> None:
    """A bad key or missing credit is not a transient failure."""
    attempts: list[int] = []
    sleeps: list[float] = []

    def poster(*_args: object) -> dict[str, object]:
        attempts.append(1)
        raise JevSetupError("invalid credential")

    classifier = JevClassifier(
        make_config(max_attempts=3),
        poster=poster,  # type: ignore[arg-type]
        sleeper=sleeps.append,
    )

    with pytest.raises(JevSetupError):
        classifier.classify("IT Recruiter")
    assert len(attempts) == 1
    assert sleeps == []


def test_classify_rejects_a_malformed_response() -> None:
    """A response without the declared Noul answers is a runtime failure."""
    classifier = JevClassifier(
        make_config(),
        poster=lambda *_args: {"answers": {}},  # type: ignore[arg-type]
    )

    with pytest.raises(JevRuntimeError):
        classifier.classify("IT Recruiter")


def test_cache_round_trip_and_reload(tmp_path: Path) -> None:
    """A decision survives a new cache instance."""
    path = tmp_path / "decisions.json"
    decision = RelevanceDecision(0.9, 0.8, 0.7, "jev-test")
    cache = JsonDecisionCache(path, "jev-test")
    cache.put("IT Recruiter\n\nAcme\n\nConnect", decision)

    reloaded = JsonDecisionCache(path, "jev-test").get("IT Recruiter\n\nAcme")

    assert reloaded == decision


def test_cache_ignores_mutable_mutual_connection_lines(tmp_path: Path) -> None:
    """Social-proof lines do not turn the same profile into a cache miss."""
    text_a = "Recruiter at Acme\n\nConnect\n\nAlice is a mutual connection"
    text_b = "Recruiter at Acme\n\nConnect\n\nBob and 3 other mutual connections"

    assert decision_key("m", text_a) == decision_key("m", text_b)


def test_cache_ignores_a_corrupt_file(tmp_path: Path) -> None:
    """A corrupt cache starts empty instead of stopping the run."""
    path = tmp_path / "decisions.json"
    path.write_text("{not json", encoding="utf-8")

    cache = JsonDecisionCache(path, "jev-test")

    assert cache.get("anything") is None
    cache.put("anything", RelevanceDecision(0.9, 0.8, 0.7, "jev-test"))
    assert JsonDecisionCache(path, "jev-test").get("anything") is not None


def test_cached_classifier_calls_the_model_once(tmp_path: Path) -> None:
    """A cache hit avoids a second Jev call for the same card."""
    fake = FakeClassifier([it_decision(is_hr=0.95, it_related=0.9, evidence=0.8)])
    cache = JsonDecisionCache(tmp_path / "decisions.json", "jev-test")
    classifier = CachedClassifier(fake, cache)

    first = classifier.classify("IT Recruiter\n\nAcme\n\nConnect")
    second = classifier.classify("IT Recruiter\n\nAcme\n\nConnect")

    assert (first.is_hr, first.it_related, first.evidence) == (
        second.is_hr,
        second.it_related,
        second.evidence,
    )
    assert second.model == "jev-test"
    assert len(fake.texts) == 1
