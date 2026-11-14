"""JSON-file persistence for the authenticated cookie jar."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRIVATE_FILE_MODE = 0o600
"""Owner read/write only: the jar is a bearer credential."""

JSON_INDENT = 2


class JsonCookieStore:
    """Keep the cookie jar as a JSON list in a file on disk.

    Reads are defensive: a missing, empty, malformed or unexpected file is
    reported through ``None`` so the caller can fall back to a manual login
    instead of crashing. Writes are atomic (temp file plus ``os.replace``) so an
    interrupted run never leaves a half-written cookie jar behind.
    """

    def __init__(self, path: Path) -> None:
        """Use *path* as the cookie jar file."""
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The file this store reads from and writes to."""
        return self._path

    def load(self) -> list[dict[str, Any]] | None:
        """Return the saved cookies, or ``None`` when none usable are available."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read cookie file %s: %s", self._path, exc)
            return None

        if not raw.strip():
            return None

        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("Cookie file %s is not valid JSON; ignoring it.", self._path)
            return None

        if not isinstance(data, list) or not data:
            logger.warning(
                "Cookie file %s does not contain a cookie list; ignoring it.",
                self._path,
            )
            return None
        if not all(isinstance(cookie, dict) for cookie in data):
            logger.warning("Cookie file %s contains non-object entries; ignoring it.", self._path)
            return None

        return data

    def save(self, cookies: Sequence[dict[str, Any]]) -> None:
        """Persist *cookies* atomically and owner-only, ignoring an empty jar."""
        if not cookies:
            logger.warning("Refusing to overwrite cookie file %s with an empty jar.", self._path)
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(list(cookies), indent=JSON_INDENT)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(descriptor, PRIVATE_FILE_MODE)
            except OSError:
                logger.debug("Could not restrict the cookie file mode.", exc_info=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            os.replace(temporary_path, self._path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        try:
            self._path.chmod(PRIVATE_FILE_MODE)
        except OSError:
            logger.debug("Could not restrict the cookie file mode.", exc_info=True)
