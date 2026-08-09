from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from app.config import Settings
from app.models_zimbra_observer import ZimbraObservationRun
from app.services.zimbra_observer import ZimbraObserverService, as_utc

logger = logging.getLogger(__name__)


def scheduled_datetime(
    schedule_time: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> datetime:
    tz = ZoneInfo(timezone_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    hour, minute = (int(part) for part in schedule_time.split(":", 1))
    return datetime.combine(
        current.date(),
        dt_time(hour=hour, minute=minute),
        tzinfo=tz,
    )


class ZimbraObserverScheduler:
    """Ежедневная read-only проверка с catch-up после запуска контейнера."""

    POLL_SECONDS = 30
    FAILURE_RETRY_MINUTES = 10

    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="zimbra-observer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _already_ran_today(self, db, now_local: datetime) -> bool:
        row = db.scalars(
            select(ZimbraObservationRun)
            .where(ZimbraObservationRun.trigger == "scheduled")
            .order_by(desc(ZimbraObservationRun.started_at), desc(ZimbraObservationRun.id))
            .limit(1)
        ).first()
        if row is None:
            return False
        started_at = as_utc(row.started_at)
        if started_at is None:
            return False
        if started_at.astimezone(now_local.tzinfo).date() != now_local.date():
            return False
        if row.status != "failed":
            return True

        # Временный сбой чтения Zimbra не лишает нас проверки на весь день,
        # но и не должен запускать тяжелый gaa каждые 30 секунд.
        finished_at = as_utc(row.completed_at) or started_at
        elapsed_minutes = (
            now_local.astimezone(finished_at.tzinfo) - finished_at
        ).total_seconds() / 60.0
        return elapsed_minutes < self.FAILURE_RETRY_MINUTES

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self.session_factory() as db:
                    service = ZimbraObserverService(self.settings, db)
                    config = service.get_settings_record()
                    if config.enabled:
                        tz = ZoneInfo(self.settings.app_timezone)
                        now_local = datetime.now(tz)
                        due = scheduled_datetime(
                            config.schedule_time,
                            self.settings.app_timezone,
                            now=now_local,
                        )
                        if now_local >= due and not self._already_ran_today(
                            db, now_local
                        ):
                            result = service.run(trigger="scheduled")
                            logger.info(
                                "Наблюдение Zimbra: status=%s close=%s archive=%s events=%s",
                                result.status,
                                result.close_candidates,
                                result.archive_candidates,
                                result.event_count,
                            )
            except Exception:
                logger.exception("Фоновая проверка Zimbra завершилась ошибкой")

            if self._stop_event.wait(self.POLL_SECONDS):
                break
