from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import desc, select

from app.config import Settings
from app.models_synology import SynologySyncRun
from app.services.blocking_window import BLOCK_RETRY_MINUTES
from app.services.synology_lifecycle import SynologyLifecycleService


logger = logging.getLogger(__name__)


class SynologyLifecycleScheduler:
    """Периодическая сверка DSM и применение разрешенных блокировок."""

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

    @staticmethod
    def _effective_interval(service, control) -> int:
        """Интервал до следующей сверки с учетом окна блокировок.

        В обычное время достаточно настроенной периодичности. Но пока окно
        плановых блокировок открыто и есть незакрытая работа, сверка должна
        приходить не реже интервала повтора — иначе редкая сверка растянула бы
        повторные попытки на весь вечер.
        """
        interval = int(control.sync_interval_minutes)
        if not control.write_enabled:
            return interval
        window_open, _reason = service.block_window_state(control)
        if window_open and service.pending_disable_count():
            return min(interval, BLOCK_RETRY_MINUTES)
        return interval

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.settings.synology_enabled:
                    with self.session_factory() as db:
                        service = SynologyLifecycleService(self.settings, db)
                        control = service.control_settings()
                        if self._due(
                            db,
                            self._effective_interval(service, control),
                        ):
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
