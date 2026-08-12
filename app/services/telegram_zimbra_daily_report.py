from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog
from app.models_dismissal_lifecycle import (
    FinalDismissalBlockRun,
    FinalDismissalBlockTarget,
)
from app.models_zimbra_lifecycle import ZimbraLifecycleAction
from app.models_zimbra_observer import (
    ZimbraLifecycleState,
    ZimbraObservationEvent,
    ZimbraObserverSettings,
)
from app.services.telegram import TelegramService


REPORT_TIME = time(8, 45)
REPORT_TIME_LABEL = "08:45"
MAX_TELEGRAM_CHARS = 3900
ARM_ACTION = "telegram_zimbra_daily_report_armed"
REPORT_ACTION = "telegram_zimbra_daily_report"
EVENT_TYPE = "zimbra_daily_report"
DISMISSAL_RE = re.compile(r"Увольнение\s+(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def local_day_bounds(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def format_date(value: date | datetime | None, timezone_name: str) -> str:
    if value is None:
        return "дата не определена"
    if isinstance(value, datetime):
        current = as_utc(value)
        assert current is not None
        value = current.astimezone(ZoneInfo(timezone_name)).date()
    return value.strftime("%d.%m.%Y")


@dataclass(frozen=True)
class DisabledEntry:
    email: str
    reason: str
    reason_kind: str


@dataclass(frozen=True)
class DeletedEntry:
    email: str
    deleted_on: date


@dataclass(frozen=True)
class ZimbraDailyReport:
    report_date: date
    disabled: tuple[DisabledEntry, ...]
    deleted: tuple[DeletedEntry, ...]
    retention_months: int

    @property
    def has_events(self) -> bool:
        return bool(self.disabled or self.deleted)


class TelegramZimbraDailyReportService:
    """Утренний агрегированный отчет только по фактически выполненным действиям."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.settings.app_timezone)

    def _armed_on(self, local_today: date) -> date:
        row = self.db.scalar(
            select(AuditLog)
            .where(AuditLog.action == ARM_ACTION)
            .order_by(AuditLog.id)
            .limit(1)
        )
        if row is not None:
            try:
                return date.fromisoformat(str(row.target or "").strip())
            except ValueError:
                return local_today

        self.db.add(
            AuditLog(
                actor="system",
                action=ARM_ACTION,
                target=local_today.isoformat(),
                result="enabled",
                details=(
                    f"report_time={REPORT_TIME_LABEL}; "
                    "period=previous_local_day; historical_backfill=false"
                ),
            )
        )
        self.db.commit()
        return local_today

    def _already_done(self, report_date: date) -> bool:
        return (
            self.db.scalar(
                select(AuditLog.id)
                .where(
                    AuditLog.action == REPORT_ACTION,
                    AuditLog.target == report_date.isoformat(),
                    AuditLog.result.in_(("queued", "empty")),
                )
                .limit(1)
            )
            is not None
        )

    def _observer_retention_months(self) -> int:
        row = self.db.get(ZimbraObserverSettings, 1)
        if row is None:
            return 12
        return max(1, int(row.retention_months or 12))

    def _dismissal_closures(
        self,
        report_date: date,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[list[DisabledEntry], set[str], set[str]]:
        rows = self.db.execute(
            select(FinalDismissalBlockTarget, FinalDismissalBlockRun)
            .join(
                FinalDismissalBlockRun,
                FinalDismissalBlockRun.id == FinalDismissalBlockTarget.run_id,
            )
            .where(
                FinalDismissalBlockTarget.system == "zimbra",
                FinalDismissalBlockTarget.status == "completed",
                FinalDismissalBlockTarget.last_result == "closed",
                FinalDismissalBlockTarget.completed_at >= start_utc,
                FinalDismissalBlockTarget.completed_at < end_utc,
            )
            .order_by(FinalDismissalBlockTarget.completed_at, FinalDismissalBlockTarget.id)
        ).all()

        result: list[DisabledEntry] = []
        seen_emails: set[str] = set()
        seen_zimbra_ids: set[str] = set()
        for target, run in rows:
            email = str(target.target_identifier or "").strip().lower()
            zimbra_id = str(target.stable_id or "").strip().lower()
            if not email:
                continue
            if email in seen_emails or (zimbra_id and zimbra_id in seen_zimbra_ids):
                continue
            seen_emails.add(email)
            if zimbra_id:
                seen_zimbra_ids.add(zimbra_id)
            result.append(
                DisabledEntry(
                    email=email,
                    reason=f"Увольнение {run.dismissal_date.strftime('%d.%m.%Y')}",
                    reason_kind="dismissal",
                )
            )
        return result, seen_emails, seen_zimbra_ids

    def _latest_close_event(
        self,
        action: ZimbraLifecycleAction,
    ) -> ZimbraObservationEvent | None:
        completed = as_utc(action.completed_at) or as_utc(action.created_at) or utcnow()
        return self.db.scalar(
            select(ZimbraObservationEvent)
            .where(
                ZimbraObservationEvent.account_key == action.account_key,
                ZimbraObservationEvent.recommendation == "close",
                ZimbraObservationEvent.created_at <= completed,
            )
            .order_by(desc(ZimbraObservationEvent.created_at), desc(ZimbraObservationEvent.id))
            .limit(1)
        )

    def _state_for_action(
        self,
        action: ZimbraLifecycleAction,
    ) -> ZimbraLifecycleState | None:
        if action.account_key:
            row = self.db.scalar(
                select(ZimbraLifecycleState)
                .where(ZimbraLifecycleState.account_key == action.account_key)
                .limit(1)
            )
            if row is not None:
                return row
        if action.zimbra_id:
            row = self.db.scalar(
                select(ZimbraLifecycleState)
                .where(ZimbraLifecycleState.zimbra_id == action.zimbra_id)
                .limit(1)
            )
            if row is not None:
                return row
        email = str(action.primary_email or "").strip().lower()
        if email:
            return self.db.scalar(
                select(ZimbraLifecycleState)
                .where(ZimbraLifecycleState.primary_email == email)
                .limit(1)
            )
        return None

    def _lifecycle_closures(
        self,
        report_date: date,
        start_utc: datetime,
        end_utc: datetime,
        already_seen_emails: set[str],
        already_seen_zimbra_ids: set[str],
    ) -> list[DisabledEntry]:
        actions = list(
            self.db.scalars(
                select(ZimbraLifecycleAction)
                .where(
                    ZimbraLifecycleAction.action == "close",
                    ZimbraLifecycleAction.status == "success",
                    ZimbraLifecycleAction.completed_at >= start_utc,
                    ZimbraLifecycleAction.completed_at < end_utc,
                )
                .order_by(ZimbraLifecycleAction.completed_at, ZimbraLifecycleAction.id)
            ).all()
        )

        result: list[DisabledEntry] = []
        for action in actions:
            # В executor success также используется для "уже была closed".
            # Это не новое отключение и в суточный отчет не попадает.
            if str(action.message or "").strip() == "На момент выполнения уже была закрыта.":
                continue

            email = str(action.primary_email or "").strip().lower()
            zimbra_id = str(action.zimbra_id or "").strip().lower()
            if not email:
                continue
            if (
                email in already_seen_emails
                or (zimbra_id and zimbra_id in already_seen_zimbra_ids)
            ):
                continue

            event = self._latest_close_event(action)
            dismissal_match = DISMISSAL_RE.search(str(event.reason or "")) if event else None
            if dismissal_match:
                reason = f"Увольнение {dismissal_match.group(1)}"
                kind = "dismissal"
            else:
                activity = event.last_logon_at if event is not None else None
                if activity is None:
                    state = self._state_for_action(action)
                    if state is not None:
                        activity = state.last_logon_at or state.created_in_zimbra_at
                reason = f"неактивна с {format_date(activity, self.settings.app_timezone)}"
                kind = "inactive"

            already_seen_emails.add(email)
            if zimbra_id:
                already_seen_zimbra_ids.add(zimbra_id)
            result.append(
                DisabledEntry(
                    email=email,
                    reason=reason,
                    reason_kind=kind,
                )
            )
        return result

    def _deletions(
        self,
        report_date: date,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[DeletedEntry]:
        actions = list(
            self.db.scalars(
                select(ZimbraLifecycleAction)
                .where(
                    ZimbraLifecycleAction.action == "delete",
                    ZimbraLifecycleAction.status == "success",
                    ZimbraLifecycleAction.completed_at >= start_utc,
                    ZimbraLifecycleAction.completed_at < end_utc,
                )
                .order_by(ZimbraLifecycleAction.completed_at, ZimbraLifecycleAction.id)
            ).all()
        )
        result: list[DeletedEntry] = []
        seen: set[str] = set()
        for action in actions:
            email = str(action.primary_email or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            completed = as_utc(action.completed_at)
            deleted_on = (
                completed.astimezone(self.timezone).date()
                if completed is not None
                else report_date
            )
            result.append(DeletedEntry(email=email, deleted_on=deleted_on))
        return result

    def collect(self, report_date: date) -> ZimbraDailyReport:
        start_utc, end_utc = local_day_bounds(report_date, self.settings.app_timezone)

        disabled, seen_emails, seen_zimbra_ids = self._dismissal_closures(
            report_date,
            start_utc,
            end_utc,
        )
        disabled.extend(
            self._lifecycle_closures(
                report_date,
                start_utc,
                end_utc,
                seen_emails,
                seen_zimbra_ids,
            )
        )
        disabled.sort(
            key=lambda item: (
                0 if item.reason_kind == "dismissal" else 1,
                item.email,
            )
        )

        deleted = self._deletions(report_date, start_utc, end_utc)
        deleted.sort(key=lambda item: item.email)

        return ZimbraDailyReport(
            report_date=report_date,
            disabled=tuple(disabled),
            deleted=tuple(deleted),
            retention_months=self._observer_retention_months(),
        )

    @staticmethod
    def _deletion_header(retention_months: int) -> str:
        if retention_months == 12:
            return (
                "Удалены следующие учетные записи по сроку давности "
                "(не используются более одного (1) года):"
            )
        if retention_months % 12 == 0:
            years = retention_months // 12
            return (
                "Удалены следующие учетные записи по сроку давности "
                f"(не используются более {years} г.):"
            )
        return (
            "Удалены следующие учетные записи по сроку давности "
            f"(не используются более {retention_months} мес.):"
        )

    @staticmethod
    def _safe_line(text: str) -> str:
        return html.escape(str(text or ""), quote=False)

    def _sections(self, report: ZimbraDailyReport) -> list[tuple[str, list[str]]]:
        sections: list[tuple[str, list[str]]] = []
        if report.disabled:
            header = (
                "Отключены следующие учетные записи за "
                f"{report.report_date.strftime('%d.%m.%Y')}:"
            )
            lines = [
                f"• {self._safe_line(item.email)} – {self._safe_line(item.reason)}"
                for item in report.disabled
            ]
            sections.append((header, lines))

        if report.deleted:
            header = self._deletion_header(report.retention_months)
            lines = [
                (
                    f"• {self._safe_line(item.email)} "
                    f"– удалено: {item.deleted_on.strftime('%d.%m.%Y')}"
                )
                for item in report.deleted
            ]
            sections.append((header, lines))
        return sections

    def render_messages(self, report: ZimbraDailyReport) -> list[str]:
        if not report.has_events:
            return []

        title = (
            "📋 <b>Учетные записи Zimbra за "
            f"{report.report_date.strftime('%d.%m.%Y')}</b>"
        )
        chunks: list[str] = []
        current = title

        for header, lines in self._sections(report):
            section_header = f"<b>{self._safe_line(header)}</b>"
            candidate = f"{current}\n\n{section_header}"
            if len(candidate) > MAX_TELEGRAM_CHARS and current != title:
                chunks.append(current)
                current = f"{title}\n\n{section_header}"
            else:
                current = candidate

            continued = False
            for line in lines:
                candidate = f"{current}\n{line}"
                if len(candidate) <= MAX_TELEGRAM_CHARS:
                    current = candidate
                    continue

                chunks.append(current)
                suffix = " (продолжение)" if continued or lines else " (продолжение)"
                current = (
                    f"{title}\n\n"
                    f"<b>{self._safe_line(header)}{suffix}</b>\n{line}"
                )
                continued = True

        if current and current != title:
            chunks.append(current)

        total = len(chunks)
        if total <= 1:
            return chunks

        result: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            marker = f"<b>Часть {index}/{total}</b>\n"
            # MAX_TELEGRAM_CHARS оставляет достаточный запас под marker.
            result.append(marker + chunk)
        return result

    def _mark_done(
        self,
        report: ZimbraDailyReport,
        *,
        result: str,
        chunks: int,
    ) -> None:
        self.db.add(
            AuditLog(
                actor="system",
                action=REPORT_ACTION,
                target=report.report_date.isoformat(),
                result=result,
                details=(
                    f"disabled={len(report.disabled)}; "
                    f"deleted={len(report.deleted)}; chunks={chunks}"
                ),
            )
        )
        self.db.commit()

    def enqueue_due(self, *, now: datetime | None = None) -> dict[str, int | str]:
        current = now or utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        local_now = current.astimezone(self.timezone)
        armed_on = self._armed_on(local_now.date())

        if local_now.time().replace(tzinfo=None) < REPORT_TIME:
            return {"status": "not_due", "queued": 0}

        report_date = local_now.date() - timedelta(days=1)
        if report_date < armed_on:
            return {"status": "not_armed_for_date", "queued": 0}

        if self._already_done(report_date):
            return {"status": "already_done", "queued": 0}

        telegram = TelegramService(self.settings.app_secret_key, self.db)
        view = telegram.view()
        if not view.enabled or not view.configured:
            return {"status": "telegram_disabled", "queued": 0}

        report = self.collect(report_date)
        messages = self.render_messages(report)
        if not messages:
            self._mark_done(report, result="empty", chunks=0)
            return {"status": "empty", "queued": 0}

        for index, message in enumerate(messages, start=1):
            telegram.enqueue(
                message,
                event_type=EVENT_TYPE,
                dedupe_key=(
                    f"{EVENT_TYPE}:{report_date.isoformat()}:{index}"
                ),
                parse_mode="HTML",
            )

        self._mark_done(report, result="queued", chunks=len(messages))
        return {"status": "queued", "queued": len(messages)}
