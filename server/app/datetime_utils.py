from __future__ import annotations

from datetime import datetime, timezone


def ensure_utc(value: datetime | None) -> datetime | None:
    """Return an aware UTC datetime for values read from the database.

    SQLite discards timezone information during a round trip. Datetimes stored
    by this application are UTC, so a naive value read back from SQLite must be
    interpreted as UTC rather than as the Central Server's local timezone.
    """

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
