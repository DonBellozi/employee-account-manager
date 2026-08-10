from __future__ import annotations

import hashlib
import logging
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from app.config import Settings
from app.models import OneCImportRun
from app.models_onec_polling import OneCSourcePollState
from app.models_onec_sources import OneCAdditionalSource
from app.services.onec_additional_import import OneCAdditionalImportService
from app.services.onec_freshness import CONTROL_EXPORT_TIME_LABEL
from app.services.onec_imap import OneCAttachment, OneCImapService
from app.services.onec_import import OneCImportService
from app.services.onec_sources import OneCSourceRegistryService
from app.services.onec_xlsx import parse_onec_xlsx


logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5 * 60
POLL_INTERVAL_LABEL = "5 минут"
FAILED_RETRY_MINUTES = 15
ACCEPTED_IMPORT_STATUSES = {"success", "partial", "duplicate"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uid_number(value: str | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _message_datetime(value: str, timezone_name: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def next_scheduled_run(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> datetime:
    tz = ZoneInfo(settings.app_timezone)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    return current + timedelta(seconds=POLL_INTERVAL_SECONDS)


def schedule_info(settings: Settings) -> dict[str, str | bool]:
    next_run = next_scheduled_run(settings)
    return {
        "enabled": settings.onec_auto_import_enabled,
        "enabled_label": (
            "Включен"
            if settings.onec_auto_import_enabled
            else "Отключен"
        ),
        "time": f"каждые {POLL_INTERVAL_LABEL}",
        "timezone": settings.app_timezone,
        "startup_catchup": settings.onec_auto_import_startup_catchup,
        "startup_catchup_label": (
            "Да"
            if settings.onec_auto_import_startup_catchup
            else "Нет"
        ),
        "next_run": next_run.strftime("%d.%m.%Y %H:%M"),
        "control_export_time": CONTROL_EXPORT_TIME_LABEL,
    }


class OneCAutoImportScheduler:
    """Круглосуточный read-only IMAP polling всех включенных источников 1С."""

    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.onec_auto_import_enabled:
            logger.info("Автоматический опрос 1С/IMAP отключен")
            return
        if self._thread is not None and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run_loop,
            name="onec-imap-polling",
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

    @staticmethod
    def _source_config_key(source: OneCAdditionalSource) -> str:
        raw = "\n".join(
            (
                source.imap_folder.strip(),
                source.sender_filter.strip(),
                source.attachment_filename.strip(),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _poll_state(cls, db, source: OneCAdditionalSource) -> OneCSourcePollState:
        config_key = cls._source_config_key(source)
        row = db.scalar(
            select(OneCSourcePollState).where(
                OneCSourcePollState.source_id == source.source_id
            )
        )
        if row is None:
            row = OneCSourcePollState(
                source_id=source.source_id,
                source_name=source.name,
                config_key=config_key,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

        row.source_name = source.name
        if row.config_key != config_key:
            # При смене папки/отправителя/имени файла UID старого IMAP-набора
            # использовать нельзя. Новый источник перечитывается с нуля.
            row.config_key = config_key
            row.last_scanned_uid = ""
            row.last_attachment_uid = ""
            row.last_file_hash = ""
            row.last_message_date = ""
            row.last_message_at = None
            row.last_status = "reset"
            row.last_error = "Настройки источника изменены; IMAP-курсор сброшен"
            row.last_success_at = None
            row.failed_uid = ""
            row.failed_attempts = 0
            row.next_retry_at = None
        db.commit()
        return row

    @staticmethod
    def _latest_accepted_run(db, source_id: str) -> OneCImportRun | None:
        return db.scalar(
            select(OneCImportRun)
            .where(
                OneCImportRun.source_id == source_id,
                OneCImportRun.status.in_(ACCEPTED_IMPORT_STATUSES),
            )
            .order_by(
                desc(OneCImportRun.completed_at),
                desc(OneCImportRun.id),
            )
            .limit(1)
        )

    @staticmethod
    def _latest_count_run(db, source_id: str) -> OneCImportRun | None:
        # У primary duplicate-запуск старой реализации мог иметь workers_count=0,
        # поэтому для контроля резкого падения берем последний реальный импорт.
        return db.scalar(
            select(OneCImportRun)
            .where(
                OneCImportRun.source_id == source_id,
                OneCImportRun.status.in_(("success", "partial")),
                OneCImportRun.workers_count > 0,
            )
            .order_by(
                desc(OneCImportRun.completed_at),
                desc(OneCImportRun.id),
            )
            .limit(1)
        )

    def _validate_attachment_snapshot(
        self,
        attachment: OneCAttachment,
        previous_run: OneCImportRun | None,
    ) -> int:
        """Fail-closed проверка полного XLSX до применения кадровых отсутствий."""
        handle = tempfile.NamedTemporaryFile(
            prefix="onec_poll_",
            suffix=".xlsx",
            delete=False,
        )
        path = Path(handle.name)
        try:
            with handle:
                handle.write(attachment.payload)
                handle.flush()

            workbook = parse_onec_xlsx(
                path,
                hash_secret=(
                    self.settings.onec_worker_hash_secret.strip()
                    or self.settings.app_secret_key
                ),
                header_search_rows=self.settings.onec_header_search_rows,
            )
            current_count = len(workbook.workers)
            previous_count = int(
                previous_run.workers_count
                if previous_run is not None
                else 0
            )

            # Защита от частично сформированного/обрезанного полного отчета.
            # Резкое сокращение более чем наполовину не применяется автоматически.
            if previous_count >= 2 and current_count * 2 < previous_count:
                raise ValueError(
                    "Новая выгрузка содержит подозрительно мало работников: "
                    f"{current_count} вместо предыдущих {previous_count}. "
                    "Кадровое состояние не изменено."
                )
            return current_count
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _record_duplicate_run(
        db,
        source: OneCAdditionalSource,
        attachment: OneCAttachment,
        previous: OneCImportRun,
    ) -> OneCImportRun:
        run = OneCImportRun(
            trigger="scheduled",
            status="duplicate",
            source_id=source.source_id,
            mail_uid=attachment.uid,
            message_date=attachment.message_date,
            sender=attachment.sender,
            subject=attachment.subject,
            filename=attachment.filename,
            file_hash=attachment.file_hash,
            workers_count=int(previous.workers_count or 0),
            placements_count=int(previous.placements_count or 0),
            message=(
                "Новая кадровая выгрузка получена; содержимое XLSX "
                "не изменилось, повторная синхронизация не выполнялась."
            ),
            started_at=utcnow(),
            completed_at=utcnow(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def _mark_success(
        db,
        state: OneCSourcePollState,
        *,
        uid: str,
        file_hash: str,
        message_date: str,
        status: str,
        scanned_uid: str,
        timezone_name: str,
    ) -> None:
        state.last_scanned_uid = str(
            max(
                _uid_number(state.last_scanned_uid),
                _uid_number(scanned_uid),
                _uid_number(uid),
            )
        )
        state.last_attachment_uid = uid
        state.last_file_hash = file_hash
        state.last_message_date = message_date
        state.last_message_at = _message_datetime(
            message_date,
            timezone_name,
        )
        state.last_status = status
        state.last_error = ""
        state.last_checked_at = utcnow()
        state.last_success_at = utcnow()
        state.failed_uid = ""
        state.failed_attempts = 0
        state.next_retry_at = None
        state.updated_at = utcnow()
        db.commit()

    @staticmethod
    def _mark_scan_only(
        db,
        state: OneCSourcePollState,
        *,
        scanned_uid: str,
    ) -> None:
        if _uid_number(scanned_uid) > _uid_number(state.last_scanned_uid):
            state.last_scanned_uid = scanned_uid
        state.last_checked_at = utcnow()
        state.updated_at = utcnow()
        db.commit()

    @staticmethod
    def _mark_error(
        db,
        state: OneCSourcePollState,
        *,
        uid: str,
        error: Exception,
    ) -> None:
        now = utcnow()
        if state.failed_uid == uid:
            state.failed_attempts = int(state.failed_attempts or 0) + 1
        else:
            state.failed_uid = uid
            state.failed_attempts = 1
        state.last_status = "failed"
        state.last_error = str(error)[:4000]
        state.last_checked_at = now
        state.next_retry_at = now + timedelta(minutes=FAILED_RETRY_MINUTES)
        state.updated_at = now
        db.commit()

    @staticmethod
    def _retry_allowed(
        state: OneCSourcePollState,
        attachment_uid: str,
    ) -> bool:
        if state.failed_uid != attachment_uid:
            return True
        if state.next_retry_at is None:
            return True
        retry_at = state.next_retry_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return retry_at <= utcnow()

    def _scan_source(
        self,
        db,
        source: OneCAdditionalSource,
    ) -> None:
        state = self._poll_state(db, source)
        state.last_checked_at = utcnow()
        db.commit()

        scan = OneCImapService(self.settings).scan_newest_attachment(
            after_uid=state.last_scanned_uid,
            folder=source.imap_folder,
            sender_filter=source.sender_filter,
            attachment_filename=source.attachment_filename,
        )
        attachment = scan.attachment
        if attachment is None:
            self._mark_scan_only(
                db,
                state,
                scanned_uid=scan.max_uid,
            )
            return

        latest = self._latest_accepted_run(db, source.source_id)
        latest_uid = _uid_number(latest.mail_uid if latest is not None else "")
        attachment_uid = _uid_number(attachment.uid)

        # Ручной импорт мог уже обработать это или более новое письмо.
        if latest is not None and latest_uid >= attachment_uid:
            self._mark_success(
                db,
                state,
                uid=latest.mail_uid or attachment.uid,
                file_hash=latest.file_hash or attachment.file_hash,
                message_date=latest.message_date or attachment.message_date,
                status=latest.status,
                scanned_uid=scan.max_uid,
                timezone_name=self.settings.app_timezone,
            )
            return

        # Новое письмо может содержать тот же полный снимок. Это свежая
        # контрольная выгрузка, но повторно синхронизировать реестр не нужно.
        if latest is not None and latest.file_hash == attachment.file_hash:
            count_source = (
                latest
                if int(latest.workers_count or 0) > 0
                else self._latest_count_run(db, source.source_id) or latest
            )
            duplicate_run = self._record_duplicate_run(
                db,
                source,
                attachment,
                count_source,
            )
            self._mark_success(
                db,
                state,
                uid=duplicate_run.mail_uid,
                file_hash=duplicate_run.file_hash,
                message_date=duplicate_run.message_date,
                status="duplicate",
                scanned_uid=scan.max_uid,
                timezone_name=self.settings.app_timezone,
            )
            return

        if not self._retry_allowed(state, attachment.uid):
            return

        try:
            baseline = self._latest_count_run(db, source.source_id)
            self._validate_attachment_snapshot(attachment, baseline)

            if source.is_primary:
                report = OneCImportService(
                    self.settings,
                    db,
                ).analyze_latest(trigger="scheduled")
                result_status = str(
                    report.get("import_status") or "success"
                )
            else:
                report = OneCAdditionalImportService(
                    self.settings,
                    db,
                    source,
                ).analyze_latest(trigger="scheduled")
                result_status = str(report.get("status") or "success")

            accepted = self._latest_accepted_run(db, source.source_id)
            if accepted is None:
                raise RuntimeError(
                    "После импорта не найдено подтвержденное состояние источника"
                )
            if result_status not in ACCEPTED_IMPORT_STATUSES:
                raise RuntimeError(
                    f"Импорт завершился неподтвержденным статусом {result_status}"
                )

            self._mark_success(
                db,
                state,
                uid=accepted.mail_uid or attachment.uid,
                file_hash=accepted.file_hash or attachment.file_hash,
                message_date=accepted.message_date or attachment.message_date,
                status=accepted.status,
                scanned_uid=scan.max_uid,
                timezone_name=self.settings.app_timezone,
            )
            logger.info(
                "Новая кадровая выгрузка %s принята: UID=%s, статус=%s",
                source.source_id,
                accepted.mail_uid or attachment.uid,
                accepted.status,
            )
        except Exception as exc:
            db.rollback()
            state = self._poll_state(db, source)
            self._mark_error(
                db,
                state,
                uid=attachment.uid,
                error=exc,
            )
            logger.exception(
                "Автоопрос 1С %s не применил новую выгрузку",
                source.source_id,
            )

    def _run_poll(self) -> None:
        with self.session_factory() as db:
            if not self._imap_configured():
                logger.warning(
                    "Автоопрос 1С пропущен: IMAP-подключение не настроено"
                )
                return

            registry = OneCSourceRegistryService(self.settings, db)
            primary = registry.primary_source()
            registry.apply_primary_to_settings(primary)
            sources = registry.enabled_sources(include_primary=True)
            if not sources:
                logger.warning(
                    "Автоопрос 1С пропущен: нет включенных источников"
                )
                return

            for source in sources:
                if (
                    not source.mail_domain.strip()
                    or not source.attachment_filename.strip()
                ):
                    continue
                try:
                    self._scan_source(db, source)
                except FileNotFoundError:
                    logger.info(
                        "Для источника %s новых кадровых выгрузок нет",
                        source.source_id,
                    )
                except Exception:
                    db.rollback()
                    logger.exception(
                        "Ошибка IMAP-опроса кадрового источника %s",
                        source.source_id,
                    )

    # Сохраняем имя метода для совместимости со старыми тестами/вызовами.
    def _run_import(self, trigger: str = "scheduled") -> None:
        self._run_poll()

    def _run_loop(self) -> None:
        if self.settings.onec_auto_import_startup_catchup:
            self._run_poll()

        while not self._stop_event.wait(POLL_INTERVAL_SECONDS):
            self._run_poll()
