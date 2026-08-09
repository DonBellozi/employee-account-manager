from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.onec_additional_import import OneCAdditionalImportService
from app.services.onec_import import OneCImportService
from app.services.onec_sources import OneCSourceRegistryService


logger = logging.getLogger(__name__)


def next_scheduled_run(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> datetime:
    tz = ZoneInfo(settings.app_timezone)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    hour, minute = (
        int(part)
        for part in settings.onec_auto_import_time.split(":", 1)
    )
    candidate = current.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def schedule_info(settings: Settings) -> dict[str, str | bool]:
    next_run = next_scheduled_run(settings)
    return {
        "enabled": settings.onec_auto_import_enabled,
        "enabled_label": (
            "Включен"
            if settings.onec_auto_import_enabled
            else "Отключен"
        ),
        "time": settings.onec_auto_import_time,
        "timezone": settings.app_timezone,
        "startup_catchup": settings.onec_auto_import_startup_catchup,
        "startup_catchup_label": (
            "Да"
            if settings.onec_auto_import_startup_catchup
            else "Нет"
        ),
        "next_run": next_run.strftime("%d.%m.%Y %H:%M"),
    }


class OneCAutoImportScheduler:
    """Ежедневный импорт всех включенных кадровых источников."""

    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.onec_auto_import_enabled:
            logger.info("Автоматический импорт 1С отключен")
            return
        if self._thread is not None and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run_loop,
            name="onec-auto-import",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _imap_configured(self) -> bool:
        return bool(
            self.settings.onec_imap_host
            and self.settings.onec_imap_username
            and self.settings.onec_imap_password
        )

    def _run_import(self, trigger: str) -> None:
        with self.session_factory() as db:
            if not self._imap_configured():
                logger.warning(
                    "Автоимпорт 1С пропущен: IMAP-подключение не настроено"
                )
                return

            registry = OneCSourceRegistryService(self.settings, db)
            primary = registry.primary_source()
            registry.apply_primary_to_settings(primary)
            ran_any = False

            if (
                primary.enabled
                and primary.mail_domain.strip()
                and primary.attachment_filename.strip()
            ):
                ran_any = True
                try:
                    report = OneCImportService(
                        self.settings,
                        db,
                    ).analyze_latest(trigger=trigger)
                    logger.info(
                        "Импорт 1С %s (%s): %s",
                        primary.source_id,
                        trigger,
                        report.get("import_status", "success"),
                    )
                except Exception:
                    logger.exception(
                        "Основной импорт 1С %s завершился ошибкой",
                        primary.source_id or "без домена",
                    )

            for source in registry.enabled_sources(include_primary=False):
                if not source.mail_domain.strip() or not source.attachment_filename.strip():
                    continue
                ran_any = True
                try:
                    report = OneCAdditionalImportService(
                        self.settings,
                        db,
                        source,
                    ).analyze_latest(trigger=trigger)
                    logger.info(
                        "Импорт 1С %s (%s): %s",
                        source.source_id,
                        trigger,
                        report.get("status", "success"),
                    )
                except Exception:
                    logger.exception(
                        "Импорт 1С %s завершился ошибкой",
                        source.source_id,
                    )

            if not ran_any:
                logger.warning(
                    "Автоимпорт 1С пропущен: нет включенных источников"
                )

    def _run_loop(self) -> None:
        if self.settings.onec_auto_import_startup_catchup:
            self._run_import("startup")

        while not self._stop_event.is_set():
            next_run = next_scheduled_run(self.settings)
            now = datetime.now(next_run.tzinfo)
            wait_seconds = max(
                1.0,
                (next_run - now).total_seconds(),
            )
            if self._stop_event.wait(wait_seconds):
                break
            self._run_import("scheduled")
