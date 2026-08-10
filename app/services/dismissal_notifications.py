from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EmailLoginMapping, HRSourceRecord, OneCImportRun
from app.models_notifications import DismissalEquipmentNotice
from app.models_onec_sources import OneCAdditionalSource
from app.services.dismissal_mailer import (
    DismissalMailer,
    ensure_dismissal_mail_templates,
    get_dismissal_mail_template,
)
from app.services.mailer import ensure_domain_mail_profiles
from app.services.upcoming_dismissals import UpcomingDismissalService


logger = logging.getLogger(__name__)
POLL_SECONDS = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def email_domain(value: str) -> str:
    normalized = normalize_email(value)
    return normalized.rsplit("@", 1)[1] if "@" in normalized else ""


class DismissalNotificationService:
    """Автоматическая одноразовая рассылка по окончательным увольнениям."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @property
    def today(self):
        return datetime.now(ZoneInfo(self.settings.app_timezone)).date()

    def _import_running(self) -> bool:
        return bool(
            self.db.scalar(
                select(OneCImportRun.id)
                .where(OneCImportRun.status == "running")
                .limit(1)
            )
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _sources_synchronized(self) -> bool:
        """Не отправлять между последовательными импортами разных организаций."""
        source_ids = [
            row.source_id
            for row in self.db.scalars(
                select(OneCAdditionalSource).where(
                    OneCAdditionalSource.enabled.is_(True)
                )
            ).all()
            if str(row.source_id or "").strip()
        ]
        source_ids = list(dict.fromkeys(source_ids))
        if not source_ids:
            return True

        completed: list[datetime] = []
        for source_id in source_ids:
            run = self.db.scalar(
                select(OneCImportRun)
                .where(
                    OneCImportRun.source_id == source_id,
                    OneCImportRun.status.in_(
                        ["success", "partial", "duplicate"]
                    ),
                )
                .order_by(
                    OneCImportRun.completed_at.desc(),
                    OneCImportRun.id.desc(),
                )
                .limit(1)
            )
            if run is None or run.completed_at is None:
                return False
            completed.append(self._aware_utc(run.completed_at))

        now = utcnow()
        newest = max(completed)
        oldest = min(completed)
        if now - newest > timedelta(hours=36):
            return False
        # Плановый импорт обходит источники последовательно. Допускаем
        # достаточный запас для больших файлов и временных задержек IMAP.
        return newest - oldest <= timedelta(minutes=30)

    def _profiles(self):
        return {
            profile.domain.strip().lower(): profile
            for profile in ensure_domain_mail_profiles(
                self.db,
                self.settings,
            )
        }

    @staticmethod
    def _final_source_ids(candidate: dict) -> list[str]:
        final_date = candidate["dismissal_date"]
        result = [
            str(item.get("source_id") or "").strip().lower()
            for item in candidate.get("organizations") or []
            if item.get("dismissal_date") == final_date
            and str(item.get("source_id") or "").strip()
        ]
        return list(dict.fromkeys(result))

    def _recipient_plan(
        self,
        candidate: dict,
        profiles: dict,
    ) -> tuple[str, list[dict]]:
        worker_key = candidate["worker_key"]
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key == worker_key
                )
            ).all()
        )
        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == worker_key
                )
            ).all()
        )

        final_source_ids = self._final_source_ids(candidate)
        relevant_source_ids = {
            str(item.get("source_id") or "").strip().lower()
            for item in candidate.get("organizations") or []
            if str(item.get("source_id") or "").strip()
        }
        configured_domains = set(profiles)
        default_domain = next(
            (domain for domain in final_source_ids if domain in configured_domains),
            "",
        )
        if not default_domain:
            default_domain = next(
                (
                    str(item.get("source_id") or "").strip().lower()
                    for item in candidate.get("organizations") or []
                    if str(item.get("source_id") or "").strip().lower()
                    in configured_domains
                ),
                "",
            )
        if not default_domain and configured_domains:
            default_domain = sorted(configured_domains)[0]

        email_to_zimbra_id: dict[str, str] = {}
        for mapping in mappings:
            zimbra_id = str(mapping.zimbra_id or "").strip()
            if not zimbra_id:
                continue
            for value in (mapping.source_email, mapping.zimbra_email):
                address = normalize_email(value)
                if address:
                    email_to_zimbra_id[address] = zimbra_id

        corporate_candidates: list[dict] = []
        for record in records:
            source_id = str(record.source_id or "").strip().lower()
            if relevant_source_ids and source_id not in relevant_source_ids:
                continue
            address = normalize_email(record.corporate_email)
            if address:
                corporate_candidates.append(
                    {
                        "email": address,
                        "source_id": source_id,
                    }
                )
        for mapping in mappings:
            source_id = str(mapping.source_domain or "").strip().lower()
            if relevant_source_ids and source_id not in relevant_source_ids:
                continue
            address = normalize_email(mapping.source_email)
            if address:
                corporate_candidates.append(
                    {
                        "email": address,
                        "source_id": source_id,
                    }
                )

        grouped: dict[str, list[dict]] = defaultdict(list)
        seen_raw: set[str] = set()
        for item in corporate_candidates:
            address = item["email"]
            if address in seen_raw:
                continue
            seen_raw.add(address)
            stable = email_to_zimbra_id.get(address)
            key = f"zimbra:{stable}" if stable else f"email:{address}"
            grouped[key].append(item)

        corporate: list[dict] = []
        for group in grouped.values():
            group.sort(
                key=lambda item: (
                    item["source_id"] not in final_source_ids,
                    email_domain(item["email"]) != item["source_id"],
                    item["email"],
                )
            )
            corporate.append(group[0])

        personal_addresses = sorted(
            {
                normalize_email(record.personal_email)
                for record in records
                if normalize_email(record.personal_email)
            }
        )
        corporate_addresses = {item["email"] for item in corporate}
        personal_addresses = [
            address
            for address in personal_addresses
            if address not in corporate_addresses
        ]

        recipients: list[dict] = []
        for item in sorted(corporate, key=lambda value: value["email"]):
            domain = email_domain(item["email"])
            sender_domain = domain if domain in configured_domains else default_domain
            recipients.append(
                {
                    "email": item["email"],
                    "kind": "corporate",
                    "sender_domain": sender_domain,
                    "sent": False,
                    "sent_at": "",
                    "error": "",
                }
            )
        for address in personal_addresses:
            recipients.append(
                {
                    "email": address,
                    "kind": "personal",
                    "sender_domain": default_domain,
                    "sent": False,
                    "sent_at": "",
                    "error": "",
                }
            )

        recipients = [item for item in recipients if item["sender_domain"]]
        return default_domain, recipients

    def _context(
        self,
        candidate: dict,
        recipients: list[dict],
    ) -> dict[str, str]:
        today = self.today
        dismissal_date = candidate["dismissal_date"]
        return_deadline = dismissal_date - timedelta(days=2)
        if return_deadline >= today:
            return_deadline_text = (
                f"не позднее {return_deadline.strftime('%d.%m.%Y')}"
            )
        else:
            return_deadline_text = "как можно скорее"

        organization_names = [
            str(item.get("source_name") or item.get("source_id") or "").strip()
            for item in candidate.get("organizations") or []
            if str(item.get("source_name") or item.get("source_id") or "").strip()
        ]
        corporate = next(
            (item["email"] for item in recipients if item["kind"] == "corporate"),
            "",
        )
        personal = next(
            (item["email"] for item in recipients if item["kind"] == "personal"),
            "",
        )
        organizations = ", ".join(dict.fromkeys(organization_names))
        return {
            "full_name": str(candidate.get("fio") or "").strip(),
            "dismissal_date": dismissal_date.strftime("%d.%m.%Y"),
            "return_deadline": return_deadline.strftime("%d.%m.%Y"),
            "return_deadline_text": return_deadline_text,
            "organization": organizations,
            "organizations": organizations,
            "corporate_email": corporate,
            "personal_email": personal,
        }

    @staticmethod
    def _retry_at(attempts: int) -> datetime:
        minutes = min(60, max(5, 5 * (2 ** max(0, attempts - 1))))
        return utcnow() + timedelta(minutes=minutes)

    def _load_recipients(self, notice: DismissalEquipmentNotice) -> list[dict]:
        try:
            value = json.loads(notice.recipients_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def _send_notice(
        self,
        notice: DismissalEquipmentNotice,
        candidate: dict,
        profiles: dict,
    ) -> None:
        recipients = self._load_recipients(notice)
        if not recipients:
            notice.status = "failed"
            notice.last_error = "Нет адресов для отправки уведомления"
            notice.next_attempt_at = self._retry_at(notice.attempts + 1)
            notice.attempts += 1
            self.db.commit()
            return

        context = self._context(candidate, recipients)
        mailer = DismissalMailer(self.settings)
        notice.attempts = int(notice.attempts or 0) + 1
        errors: list[str] = []

        for recipient in recipients:
            if recipient.get("sent"):
                continue
            address = normalize_email(recipient.get("email"))
            sender_domain = str(recipient.get("sender_domain") or "").strip().lower()
            profile = profiles.get(sender_domain)
            if profile is None:
                message = f"Не настроен профиль отправителя для {sender_domain or 'домена'}"
                recipient["error"] = message
                errors.append(f"{address}: {message}")
                continue
            try:
                template = get_dismissal_mail_template(
                    self.db,
                    self.settings,
                    sender_domain,
                )
                mailer.send_notice(
                    template=template,
                    sender_email=profile.sender_email,
                    sender_name=profile.sender_name,
                    recipient=address,
                    context=context,
                )
                recipient["sent"] = True
                recipient["sent_at"] = utcnow().isoformat()
                recipient["error"] = ""
            except Exception as exc:
                recipient["error"] = str(exc)
                errors.append(f"{address}: {exc}")

        sent_count = sum(1 for item in recipients if item.get("sent"))
        total = len(recipients)
        notice.recipients_json = json.dumps(
            recipients,
            ensure_ascii=False,
            sort_keys=True,
        )
        notice.last_error = "\n".join(errors)[:4000]
        notice.updated_at = utcnow()

        if sent_count == total:
            notice.status = "sent"
            notice.sent_at = utcnow()
            notice.next_attempt_at = None
        elif sent_count:
            notice.status = "partial"
            notice.next_attempt_at = self._retry_at(notice.attempts)
        else:
            notice.status = "failed"
            notice.next_attempt_at = self._retry_at(notice.attempts)
        self.db.commit()

    def process(self) -> dict[str, int | str]:
        if self.settings.dry_run:
            return {"status": "dry_run", "created": 0, "sent": 0}
        if self._import_running():
            return {"status": "import_running", "created": 0, "sent": 0}
        if not self._sources_synchronized():
            return {"status": "sources_not_synchronized", "created": 0, "sent": 0}

        upcoming_service = UpcomingDismissalService(self.settings, self.db)
        candidates = [
            item
            for item in upcoming_service.list_upcoming(limit=10000)
            if item["dismissal_date"] >= self.today
        ]
        candidate_by_key = {
            (item["worker_key"], item["dismissal_date"]): item
            for item in candidates
        }

        open_notices = list(
            self.db.scalars(
                select(DismissalEquipmentNotice).where(
                    DismissalEquipmentNotice.status.in_(
                        ["pending", "partial", "failed", "cancelled"]
                    )
                )
            ).all()
        )
        notice_by_key = {
            (item.worker_key, item.dismissal_date): item
            for item in open_notices
        }

        # Если человек снова стал активным до отправки, письмо отменяется.
        for key, notice in notice_by_key.items():
            if key in candidate_by_key or notice.status == "cancelled":
                continue
            notice.status = "cancelled"
            notice.cancelled_at = utcnow()
            notice.next_attempt_at = None
            notice.last_error = "Кадровая ситуация изменилась до отправки"
        self.db.commit()

        profiles = self._profiles()
        dismissal_templates = ensure_dismissal_mail_templates(
            self.db,
            self.settings,
        )
        created = 0
        sent_now = 0

        for key, candidate in candidate_by_key.items():
            notice = self.db.scalar(
                select(DismissalEquipmentNotice).where(
                    DismissalEquipmentNotice.worker_key == key[0],
                    DismissalEquipmentNotice.dismissal_date == key[1],
                )
            )
            if notice is not None and notice.status == "sent":
                continue

            if notice is None:
                sender_domain, recipients = self._recipient_plan(
                    candidate,
                    profiles,
                )
                if not recipients:
                    # Не фиксируем пустую рассылку: если адрес появится позже,
                    # следующий цикл сможет сформировать письмо.
                    continue

                # Новый тип автоматического письма сначала должен быть явно
                # просмотрен и сохранен оператором для каждого используемого
                # домена. Это исключает неожиданную рассылку сразу после
                # установки обновления.
                sender_domains = {
                    str(item.get("sender_domain") or "").strip().lower()
                    for item in recipients
                    if str(item.get("sender_domain") or "").strip()
                }
                if any(
                    domain not in dismissal_templates
                    or not str(
                        dismissal_templates[domain].updated_by or ""
                    ).strip()
                    for domain in sender_domains
                ):
                    continue

                notice = DismissalEquipmentNotice(
                    worker_key=key[0],
                    dismissal_date=key[1],
                    fio=str(candidate.get("fio") or ""),
                    sender_domain=sender_domain,
                    recipients_json=json.dumps(
                        recipients,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    status="pending",
                )
                self.db.add(notice)
                self.db.commit()
                self.db.refresh(notice)
                created += 1
            elif notice.status == "cancelled":
                # Та же дата снова стала актуальной до фактической отправки.
                notice.status = "pending"
                notice.cancelled_at = None
                notice.last_error = ""
                notice.next_attempt_at = None
                self.db.commit()

            now = utcnow()
            next_attempt_at = notice.next_attempt_at
            if next_attempt_at is not None:
                compare_now = now
                if next_attempt_at.tzinfo is None:
                    compare_now = now.replace(tzinfo=None)
                if next_attempt_at > compare_now:
                    continue
            previous_status = notice.status
            self._send_notice(notice, candidate, profiles)
            if previous_status != "sent" and notice.status == "sent":
                sent_now += 1

        return {
            "status": "ok",
            "created": created,
            "sent": sent_now,
            "candidates": len(candidates),
        }


class DismissalNotificationWorker:
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
            name="dismissal-notifications",
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
                result = DismissalNotificationService(
                    self.settings,
                    db,
                ).process()
                if result.get("sent"):
                    logger.info(
                        "Отправлено уведомлений об увольнении: %s",
                        result.get("sent"),
                    )
            except Exception:
                db.rollback()
                logger.exception(
                    "Ошибка фоновой рассылки уведомлений об увольнении"
                )

    def _run_loop(self) -> None:
        # Первый цикл откладываем: при старте приложения сначала должна
        # завершиться возможная catch-up выгрузка 1С.
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
