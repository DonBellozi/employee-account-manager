from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def app_now() -> datetime:
    """Текущее время сервера в часовом поясе приложения."""
    return datetime.now(_zone())


def app_timezone_name() -> str:
    zone = _zone()
    return str(getattr(zone, "key", MOSCOW_TIMEZONE))


def server_clock() -> dict[str, object]:
    """Снимок серверных часов для шапки интерфейса.

    Показывается именно время сервера, а не браузера: по нему считаются
    расписания воркеров и контрольное окно кадровых выгрузок после 19:00.
    """
    now = app_now()
    offset = now.utcoffset() or timedelta(0)
    offset_minutes = int(offset.total_seconds() // 60)
    sign = "+" if offset_minutes >= 0 else "-"
    hours, minutes = divmod(abs(offset_minutes), 60)
    return {
        "text": now.strftime("%d.%m.%Y %H:%M:%S"),
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M:%S"),
        "zone": app_timezone_name(),
        "offset_minutes": offset_minutes,
        "offset_label": f"UTC{sign}{hours:02d}:{minutes:02d}",
        "epoch_ms": int(now.timestamp() * 1000),
    }


def register_datetime_filters(templates: Jinja2Templates) -> Jinja2Templates:
    templates.env.filters["app_datetime"] = format_app_datetime
    templates.env.globals["server_clock"] = server_clock
    return templates
