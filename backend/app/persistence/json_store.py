"""Atomic, configuration-only JSON persistence.

Configuration stores (registry, host-key trust) are the only writers. Telemetry
modules must not use this store, so normal monitoring creates no disk writes.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger("persistence.json")


class JsonFileStore:
    """Load and atomically replace one small JSON configuration file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def load(self, default: Any) -> Any:
        if not self.path.is_file():
            return default
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            # Do not rename or otherwise mutate the user's file at startup.
            # The next explicit mutation may atomically replace it.
            logger.warning("cannot read JSON store %s; using default", self.path)
            return default

    def save(self, value: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise
