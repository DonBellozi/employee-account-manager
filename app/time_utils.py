from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

from app.config import get_settings

MOSCOW_TIMEZONE = "Europe/Moscow"


def _zone() -> ZoneInfo:
    """Return the configured application zone, falling back to Moscow."""
    name = str(get_settings().app_timezone or MOSCOW_TIMEZONE).strip() or MOSCOW_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(MOSCOW_TIMEZONE)


def to_app_timezone(value: datetime | None) -> datetime | None:
    """Convert a DB timestamp to application local time.

    SQLite drops timezone information from DateTime values. All project timestamps are
    stored from UTC-producing helpers, so a naive value read back from SQLite is UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_zone())


def format_app_datetime(
    value: datetime | None,
    fmt: str = "%d.%m.%Y %H:%M:%S",
) -> str:
    local_value = to_app_timezone(value)
    if local_value is None:
        return ""
    return local_value.strftime(fmt)


def register_datetime_filters(templates: Jinja2Templates) -> Jinja2Templates:
    templates.env.filters["app_datetime"] = format_app_datetime
    return templates
