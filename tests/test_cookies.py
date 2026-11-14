"""Tests for the JSON cookie store."""

from __future__ import annotations

import json
import logging

import pytest

from linkedin_auto_connect.cookies import JsonCookieStore

COOKIES = [
    {"name": "li_at", "value": "secret", "domain": ".linkedin.com", "path": "/"},
    {"name": "lang", "value": "v=2", "domain": ".linkedin.com", "path": "/"},
]


def test_save_and_load_roundtrip(tmp_path) -> None:
    """A saved jar is returned unchanged by ``load()``."""
    path = tmp_path / "cookies.json"
    store = JsonCookieStore(path)
    store.save(COOKIES)
    assert store.load() == COOKIES


def test_save_accepts_any_sequence(tmp_path) -> None:
    """``save()`` takes any sequence, not only a list."""
    path = tmp_path / "cookies.json"
    store = JsonCookieStore(path)
    store.save(tuple(COOKIES))
    assert store.load() == COOKIES


def test_missing_file_returns_none(tmp_path) -> None:
    """A cookie file that does not exist yields ``None``."""
    assert JsonCookieStore(tmp_path / "absent.json").load() is None


@pytest.mark.parametrize("content", ["", "   \n", "[]"])
def test_empty_file_or_empty_list_returns_none(tmp_path, content: str) -> None:
    """Empty files and empty JSON lists yield ``None``."""
    path = tmp_path / "cookies.json"
    path.write_text(content, encoding="utf-8")
    assert JsonCookieStore(path).load() is None


@pytest.mark.parametrize("content", ['{"name": "li_at"}', "null", "[1, 2]"])
def test_non_cookie_list_returns_none(tmp_path, content: str) -> None:
    """JSON that is not a list of cookie objects yields ``None``."""
    path = tmp_path / "cookies.json"
    path.write_text(content, encoding="utf-8")
    assert JsonCookieStore(path).load() is None


def test_corrupt_json_returns_none_and_warns(tmp_path, caplog) -> None:
    """Malformed JSON never raises out of ``load()``; it logs a warning."""
    path = tmp_path / "cookies.json"
    path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="linkedin_auto_connect.cookies"):
        assert JsonCookieStore(path).load() is None

    messages = [record.getMessage() for record in caplog.records]
    assert any("not valid JSON" in message for message in messages)


def test_undecodable_bytes_return_none_and_warn(tmp_path, caplog) -> None:
    """A file that is not UTF-8 text never raises out of ``load()``."""
    path = tmp_path / "cookies.json"
    path.write_bytes(b"\xff\xfe\x00not-utf8")

    with caplog.at_level(logging.WARNING, logger="linkedin_auto_connect.cookies"):
        assert JsonCookieStore(path).load() is None

    messages = [record.getMessage() for record in caplog.records]
    assert any("Could not read cookie file" in message for message in messages)


def test_empty_save_leaves_existing_file_untouched(tmp_path, caplog) -> None:
    """Saving an empty jar warns and keeps the previous cookies on disk."""
    path = tmp_path / "cookies.json"
    store = JsonCookieStore(path)
    store.save(COOKIES)
    before = path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="linkedin_auto_connect.cookies"):
        store.save([])

    assert path.read_text(encoding="utf-8") == before
    messages = [record.getMessage().lower() for record in caplog.records]
    assert any("empty" in message for message in messages)


def test_save_creates_missing_parent_directories(tmp_path) -> None:
    """Nested storage paths are created on demand."""
    path = tmp_path / "nested" / "deeper" / "cookies.json"
    store = JsonCookieStore(path)
    store.save(COOKIES)
    assert path.is_file()
    assert store.load() == COOKIES


def test_saved_file_is_indented_json(tmp_path) -> None:
    """The file on disk is valid JSON written with ``indent=2``."""
    path = tmp_path / "cookies.json"
    JsonCookieStore(path).save(COOKIES)

    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == COOKIES
    assert text.startswith("[\n  {")


def test_save_leaves_no_temporary_files_behind(tmp_path) -> None:
    """The atomic write renames its temporary file over the target."""
    path = tmp_path / "cookies.json"
    JsonCookieStore(path).save(COOKIES)
    assert list(tmp_path.iterdir()) == [path]
