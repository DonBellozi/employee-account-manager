from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import desc, select

from app.config import Settings
from app.models_synology import SynologySyncRun
from app.services.synology_lifecycle import SynologyLifecycleService


logger = logging.getLogger(__name__)


class SynologyLifecycleScheduler:
    """Периодическая read-only сверка DSM с кадровым lifecycle."""

    POLL_SECONDS = 30

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
            name="synology-lifecycle",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _due(self, db, interval_minutes: int) -> bool:
        row = db.scalar(
            select(SynologySyncRun)
            .where(SynologySyncRun.status.in_(["success", "partial"]))
            .order_by(desc(SynologySyncRun.completed_at), desc(SynologySyncRun.id))
            .limit(1)
        )
        if row is None:
            return True
        completed = self._as_utc(row.completed_at)
        if completed is None:
            return True
        elapsed = (datetime.now(timezone.utc) - completed).total_seconds() / 60.0
        return elapsed >= max(1, int(interval_minutes))

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.settings.synology_enabled:
                    with self.session_factory() as db:
                        service = SynologyLifecycleService(self.settings, db)
                        control = service.control_settings()
                        if self._due(db, control.sync_interval_minutes):
                            result = service.sync(trigger="scheduled")
                            logger.info(
                                "Synology DSM: status=%s users=%s planned=%s detail_errors=%s",
                                result.status,
                                result.users_count,
                                result.planned_actions,
                                result.detail_errors,
                            )
            except Exception:
                logger.exception("Фоновая сверка Synology завершилась ошибкой")

            if self._stop_event.wait(self.POLL_SECONDS):
                break
