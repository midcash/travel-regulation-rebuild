"""Clock port for deterministic time access."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Provide the current time to time-sensitive workflows."""

    def now(self) -> datetime:
        """Return the current time."""
        ...


class SystemClock:
    """Production clock backed by the system UTC time."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)
