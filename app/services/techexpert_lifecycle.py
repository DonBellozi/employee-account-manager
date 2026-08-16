from __future__ import annotations

import html
import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_dismissals import DismissalDeferral
from app.models_notifications import HREmploymentDismissalEvent
from app.models_onec_sources import HREmploymentState
from app.models_techexpert import TechExpertNotification
from app.services.ad import ActiveDirectoryService
from app.services.mailer import CredentialMailer, get_domain_mail_profile


logger = logging.getLogger(__name__)
POLL_SECONDS = 60
NOTIFICATION_TIME = time(8, 45)
RETRY_MINUTES = 15
ACTIVE_STATUSES = {"active"}
OPEN_STATUSES = {"pending", "deferred", "failed", "intervention"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


def email_domain(value: str) -> str:
    normalized = normalize(value)
    return normalized.rsplit("@", 1)[1] if normalized.count("@") == 1 else ""


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TechExpertDataError(RuntimeError):
    """Ошибка данных, которую должен исправить оператор."""


@dataclass(frozen=True)
class TechExpertIdentity:
    corporate_email: str
    ad_login: str
    ad_object_guid: str


class TechExpertLifecycleService:
    """Уведомляет Техэксперт, не изменяя AD-группу или внешнюю систему."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @property
    def local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.app_timezone))

    @property
    def source_domain(self) -> str:
        return normalize(self.settings.techexpert_source_domain)

    def _configuration_error(self) -> str:
        missing = []
        if not self.source_domain:
            missing.append("TECHEXPERT_SOURCE_DOMAIN")
        elif self.source_domain not in {
            normalize(domain) for domain in self.settings.zimbra_domains
        }:
            missing.append(
                "TECHEXPERT_SOURCE_DOMAIN (нет почтового профиля домена)"
            )
        if not str(self.settings.techexpert_ad_group_dn or "").strip():
            missing.append("TECHEXPERT_AD_GROUP_DN")
        if not email_domain(self.settings.techexpert_recipient_email):
            missing.append("TECHEXPERT_RECIPIENT_EMAIL")
        if not str(self.settings.smtp_host or "").strip():
            missing.append("SMTP_HOST")
        return ", ".join(missing)

    def _local(self, value: datetime) -> datetime:
        return aware_utc(value).astimezone(ZoneInfo(self.settings.app_timezone))

    def _at_notification_time(self, value: date) -> datetime:
        local = datetime.combine(
            value,
            NOTIFICATION_TIME,
            tzinfo=ZoneInfo(self.settings.app_timezone),
        )
        return local.astimezone(timezone.utc)

    def _next_notification_time(self, confirmed_at: datetime) -> datetime:
        local_confirmation = self._local(confirmed_at)
        candidate = datetime.combine(
            local_confirmation.date(),
            NOTIFICATION_TIME,
            tzinfo=local_confirmation.tzinfo,
        )
        if candidate < local_confirmation:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    def _deferral(
        self,
        worker_key: str,
        dismissal_date: date,
    ) -> DismissalDeferral | None:
        return self.db.scalar(
            select(DismissalDeferral).where(
                DismissalDeferral.worker_key == worker_key,
                DismissalDeferral.dismissal_date == dismissal_date,
            )
        )

    def _scheduled_for(
        self,
        *,
        event: HREmploymentDismissalEvent,
        state: HREmploymentState,
        deferral: DismissalDeferral | None,
    ) -> datetime:
        dismissal_date = event.current_dismissal_date
        if dismissal_date is None:
            raise TechExpertDataError("В кадровом событии отсутствует дата увольнения")

        confirmed_at = event.updated_at or event.created_at
        confirmation_date = self._local(confirmed_at).date()
        retroactive = (
            normalize(state.status_reason) == "absent_from_export"
            or dismissal_date < confirmation_date
        )
        if retroactive:
            candidate = self._next_notification_time(confirmed_at)
        else:
            candidate = self._at_notification_time(
                dismissal_date + timedelta(days=1)
            )

        if deferral is not None:
            candidate = max(
                candidate,
                self._at_notification_time(deferral.deferred_until),
            )
        return candidate

    def _employment_state(
        self,
        event: HREmploymentDismissalEvent,
    ) -> HREmploymentState | None:
        return self.db.scalar(
            select(HREmploymentState).where(
                HREmploymentState.worker_key == event.worker_key,
                HREmploymentState.source_id == self.source_domain,
            )
        )

    def _record_for_event(
        self,
        event: HREmploymentDismissalEvent,
    ) -> HRSourceRecord | None:
        return self.db.scalar(
            select(HRSourceRecord).where(
                HRSourceRecord.worker_key == event.worker_key,
                HRSourceRecord.source_id == self.source_domain,
            )
        )

    def _audit(
        self,
        row: TechExpertNotification,
        *,
        action: str,
        result: str,
        details: str = "",
    ) -> None:
        payload = {
            "notification_id": row.id,
            "worker_key": row.worker_key,
            "fio": row.fio,
            "source_id": row.source_id,
            "corporate_email": row.corporate_email,
            "recipient_email": row.recipient_email,
            "dismissal_date": row.dismissal_date.isoformat(),
            "details": details,
        }
        self.db.add(
            AuditLog(
                actor="system",
                action=action,
                target=row.corporate_email or row.worker_key,
                result=result,
                details=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )
        )

    def _mark_cancelled(
        self,
        row: TechExpertNotification,
        reason: str,
    ) -> None:
        if row.status not in OPEN_STATUSES:
            return
        row.status = "cancelled"
        row.cancelled_at = utcnow()
        row.next_attempt_at = None
        row.last_error = reason
        row.updated_at = utcnow()
        self._audit(
            row,
            action="techexpert_notification_cancelled",
            result="cancelled",
            details=reason,
        )

    def _mark_attention_after_send(
        self,
        row: TechExpertNotification,
    ) -> None:
        if row.status != "sent" or row.attention_state:
            return
        row.attention_state = "hr_active_after_notification"
        row.attention_details = (
            "После отправки запроса на прекращение доступа работник снова "
            "активен в кадровом источнике. Автоматических действий нет; "
            "нужно связаться с Техэкспертом вручную."
        )
        row.attention_at = utcnow()
        row.updated_at = utcnow()
        self._audit(
            row,
            action="techexpert_reactivation_attention",
            result="attention",
            details=row.attention_details,
        )

    def _ensure_notification(
        self,
        event: HREmploymentDismissalEvent,
        state: HREmploymentState | None,
    ) -> TechExpertNotification | None:
        row = self.db.scalar(
            select(TechExpertNotification).where(
                TechExpertNotification.employment_event_id == event.id
            )
        )

        event_is_current = (
            state is not None
            and event.current_dismissal_date is not None
            and normalize(state.status) not in ACTIVE_STATUSES
            and state.dismissal_date == event.current_dismissal_date
        )
        if not event_is_current:
            if row is not None:
                if normalize(getattr(state, "status", "")) in ACTIVE_STATUSES:
                    self._mark_attention_after_send(row)
                self._mark_cancelled(
                    row,
                    "Кадровая дата снята или работник снова активен в организации",
                )
            return row

        assert state is not None
        assert event.current_dismissal_date is not None
        deferral = self._deferral(event.worker_key, event.current_dismissal_date)
        event_updated_at = aware_utc(event.updated_at or event.created_at)
        hr_reason = normalize(state.status_reason)
        scheduled_for = self._scheduled_for(
            event=event,
            state=state,
            deferral=deferral,
        )
        record = self._record_for_event(event)
        corporate_email = normalize(
            getattr(record, "corporate_email", "")
        )

        if row is None:
            row = TechExpertNotification(
                employment_event_id=event.id,
                worker_key=event.worker_key,
                source_id=self.source_domain,
                source_name=event.source_name,
                fio=event.fio,
                corporate_email=corporate_email,
                dismissal_date=event.current_dismissal_date,
                deferred_until=(deferral.deferred_until if deferral else None),
                hr_reason=hr_reason,
                event_updated_at=event_updated_at,
                recipient_email=normalize(
                    self.settings.techexpert_recipient_email
                ),
                scheduled_for=scheduled_for,
                status=(
                    "deferred"
                    if deferral is not None
                    and deferral.deferred_until > self.local_now.date()
                    else "pending"
                ),
            )
            self.db.add(row)
            return row

        material_change = any(
            (
                row.dismissal_date != event.current_dismissal_date,
                row.deferred_until != (
                    deferral.deferred_until if deferral else None
                ),
                normalize(row.hr_reason) != hr_reason,
                aware_utc(row.event_updated_at) != event_updated_at,
            )
        )
        row.source_name = event.source_name
        row.fio = event.fio
        if row.status != "sent":
            row.corporate_email = corporate_email
            row.recipient_email = normalize(
                self.settings.techexpert_recipient_email
            )
        if material_change and row.status not in {"sent", "skipped"}:
            row.dismissal_date = event.current_dismissal_date
            row.deferred_until = deferral.deferred_until if deferral else None
            row.hr_reason = hr_reason
            row.event_updated_at = event_updated_at
            row.scheduled_for = scheduled_for
            row.next_attempt_at = None
            row.cancelled_at = None
            row.last_error = ""
            row.status = (
                "deferred"
                if deferral is not None
                and deferral.deferred_until > self.local_now.date()
                else "pending"
            )
        elif row.status == "cancelled":
            row.dismissal_date = event.current_dismissal_date
            row.deferred_until = deferral.deferred_until if deferral else None
            row.hr_reason = hr_reason
            row.event_updated_at = event_updated_at
            row.scheduled_for = scheduled_for
            row.cancelled_at = None
            row.last_error = ""
            row.status = "pending"
        row.updated_at = utcnow()
        return row

    def _resolve_identity(
        self,
        row: TechExpertNotification,
    ) -> TechExpertIdentity:
        record = self.db.scalar(
            select(HRSourceRecord).where(
                HRSourceRecord.worker_key == row.worker_key,
                HRSourceRecord.source_id == self.source_domain,
            )
        )
        corporate_email = normalize(
            getattr(record, "corporate_email", "")
            or row.corporate_email
        )
        if not corporate_email or email_domain(corporate_email) != self.source_domain:
            raise TechExpertDataError(
                "Не найден корпоративный email работника в организации Техэксперта"
            )

        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == row.worker_key
                )
            ).all()
        )
        preferred = next(
            (
                item
                for item in mappings
                if normalize(item.source_domain) == self.source_domain
            ),
            None,
        )
        if preferred is None:
            identities = {
                (
                    normalize(item.ad_object_guid),
                    normalize(item.ad_login),
                )
                for item in mappings
                if normalize(item.ad_object_guid) or normalize(item.ad_login)
            }
            if len(identities) > 1:
                raise TechExpertDataError(
                    "Найдено несколько разных учетных записей AD для работника"
                )
            if identities:
                guid, login = next(iter(identities))
            else:
                guid, login = "", normalize(getattr(record, "login", ""))
        else:
            guid = normalize(preferred.ad_object_guid)
            login = normalize(preferred.ad_login)

        if not guid and not login:
            raise TechExpertDataError(
                "Не настроено сопоставление работника с учетной записью AD"
            )
        return TechExpertIdentity(corporate_email, login, guid)

    def _current_event_and_state(
        self,
        row: TechExpertNotification,
    ) -> tuple[HREmploymentDismissalEvent | None, HREmploymentState | None]:
        event = self.db.get(
            HREmploymentDismissalEvent,
            row.employment_event_id,
        )
        if event is None:
            return None, None
        return event, self._employment_state(event)

    def _still_due(
        self,
        row: TechExpertNotification,
        event: HREmploymentDismissalEvent | None,
        state: HREmploymentState | None,
    ) -> bool:
        return bool(
            event is not None
            and state is not None
            and normalize(event.source_id) == self.source_domain
            and event.current_dismissal_date == row.dismissal_date
            and state.dismissal_date == row.dismissal_date
            and normalize(state.status) not in ACTIVE_STATUSES
        )

    def _send(self, row: TechExpertNotification) -> bool:
        event, state = self._current_event_and_state(row)
        if not self._still_due(row, event, state):
            self._mark_cancelled(row, "Повторная HR-проверка отменила письмо")
            return False

        deferral = self._deferral(row.worker_key, row.dismissal_date)
        if deferral is not None:
            deferral_time = self._at_notification_time(deferral.deferred_until)
            if deferral_time > utcnow():
                row.status = "deferred"
                row.deferred_until = deferral.deferred_until
                row.scheduled_for = max(
                    aware_utc(row.scheduled_for),
                    deferral_time,
                )
                return False

        row.attempts = int(row.attempts or 0) + 1
        row.next_attempt_at = None
        row.updated_at = utcnow()
        try:
            row.membership_state = "not_checked"
            identity = self._resolve_identity(row)
            row.corporate_email = identity.corporate_email
            row.ad_login = identity.ad_login
            row.ad_object_guid = identity.ad_object_guid

            ad = ActiveDirectoryService(self.settings)
            try:
                is_member = ad.is_user_member_of_group(
                    identity.ad_login,
                    self.settings.techexpert_ad_group_dn,
                    object_guid=identity.ad_object_guid,
                )
            except Exception:
                row.membership_state = "error"
                raise
            row.membership_state = "member" if is_member else "not_member"
            if not is_member:
                row.status = "skipped"
                row.last_error = ""
                row.updated_at = utcnow()
                self._audit(
                    row,
                    action="techexpert_notification_skipped",
                    result="not_member",
                    details="Работник не входит в маркерную группу AD",
                )
                return False

            # Вторая кадровая проверка выполняется непосредственно после AD и
            # перед SMTP. Письмо нельзя отправлять по уже отмененному событию.
            self.db.flush()
            self.db.expire_all()
            event, state = self._current_event_and_state(row)
            if not self._still_due(row, event, state):
                self._mark_cancelled(
                    row,
                    "Повторная HR-проверка перед SMTP отменила письмо",
                )
                return False
            latest_deferral = self._deferral(
                row.worker_key,
                row.dismissal_date,
            )
            if latest_deferral is not None:
                deferral_time = self._at_notification_time(
                    latest_deferral.deferred_until
                )
                if deferral_time > utcnow():
                    row.status = "deferred"
                    row.deferred_until = latest_deferral.deferred_until
                    row.scheduled_for = deferral_time
                    return False

            profile = get_domain_mail_profile(
                self.db,
                self.settings,
                self.source_domain,
            )
            fio = html.escape(row.fio or row.worker_key)
            corporate_email = html.escape(row.corporate_email)
            body = (
                "<p>Здравствуйте!</p>"
                "<p>Просим прекратить доступ к системе «Техэксперт» "
                "для следующего работника:</p>"
                f"<p><strong>ФИО:</strong> {fio}<br>"
                f"<strong>Корпоративный email:</strong> {corporate_email}</p>"
                "<p>Это автоматическое уведомление по подтвержденному "
                "кадровому событию.</p>"
            )
            CredentialMailer(self.settings).send_html(
                recipient=row.recipient_email,
                subject="Прекращение доступа к системе «Техэксперт»",
                body_html=body,
                sender_email=profile.sender_email,
                sender_name=profile.sender_name,
            )
            row.status = "sent"
            row.sent_at = utcnow()
            row.last_error = ""
            row.next_attempt_at = None
            row.updated_at = utcnow()
            self._audit(
                row,
                action="techexpert_notification_sent",
                result="success",
                details="Запрос на прекращение доступа отправлен",
            )
            return True
        except TechExpertDataError as exc:
            row.status = "intervention"
            row.last_error = str(exc)[:4000]
            row.next_attempt_at = utcnow() + timedelta(minutes=RETRY_MINUTES)
        except Exception as exc:
            row.status = "failed"
            row.last_error = str(exc)[:4000]
            row.next_attempt_at = utcnow() + timedelta(minutes=RETRY_MINUTES)
        row.updated_at = utcnow()
        self._audit(
            row,
            action="techexpert_notification_failed",
            result=row.status,
            details=row.last_error,
        )
        return False

    @staticmethod
    def _datetime_due(value: datetime | None, now: datetime) -> bool:
        if value is None:
            return True
        return aware_utc(value) <= now

    def process(self) -> dict[str, int | str]:
        if not self.settings.techexpert_enabled:
            return {"status": "disabled", "planned": 0, "sent": 0}
        configuration_error = self._configuration_error()
        if configuration_error:
            return {
                "status": "misconfigured",
                "missing": configuration_error,
                "planned": 0,
                "sent": 0,
            }

        events = list(
            self.db.scalars(
                select(HREmploymentDismissalEvent).where(
                    HREmploymentDismissalEvent.source_id == self.source_domain
                )
            ).all()
        )
        rows: list[TechExpertNotification] = []
        for event in events:
            row = self._ensure_notification(
                event,
                self._employment_state(event),
            )
            if row is not None:
                rows.append(row)
        self.db.commit()

        now = utcnow()
        sent = 0
        for row in rows:
            if row.status not in OPEN_STATUSES:
                continue
            if not self._datetime_due(row.scheduled_for, now):
                continue
            if not self._datetime_due(row.next_attempt_at, now):
                continue
            if self._send(row):
                sent += 1
            self.db.commit()
        return {"status": "ok", "planned": len(rows), "sent": sent}


class TechExpertLifecycleWorker:
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
            name="techexpert-lifecycle",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run_once(self) -> None:
        with self.session_factory() as db:
            try:
                result = TechExpertLifecycleService(
                    self.settings,
                    db,
                ).process()
                if result.get("sent"):
                    logger.info(
                        "Техэксперт: отправлено уведомлений %s",
                        result["sent"],
                    )
            except Exception:
                db.rollback()
                logger.exception("Ошибка уведомительного контура Техэксперта")

    def _run_loop(self) -> None:
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
