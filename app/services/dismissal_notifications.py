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
from app.models_notifications import (
    DismissalEquipmentNotice,
    HREmploymentDismissalEvent,
)
from app.models_onec_polling import OneCSourcePollState
from app.models_onec_sources import HREmploymentState
from app.models_onec_sources import OneCAdditionalSource
from app.services.dismissal_mailer import (
    DismissalMailer,
    ensure_dismissal_mail_templates,
    get_dismissal_mail_template,
)
from app.services.mailer import ensure_domain_mail_profiles


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

    def _source_confirmation(self) -> tuple[bool, str]:
        """Подтвердить, что все источники проверены после кадрового изменения.

        Для подтверждения не требуется новый XLSX. Достаточно успешного IMAP-
        опроса после изменения: это не заставляет один молчащий источник
        бессрочно удерживать письма остальных организаций.
        """
        sources = list(
            self.db.scalars(
                select(OneCAdditionalSource).where(
                    OneCAdditionalSource.enabled.is_(True)
                )
            ).all()
        )
        sources = [
            source
            for source in sources
            if str(source.source_id or "").strip()
        ]
        if not sources:
            return True, ""

        changed_values = [
            self._aware_utc(value)
            for value in self.db.scalars(
                select(HREmploymentState.updated_at).where(
                    HREmploymentState.dismissal_date.is_not(None)
                )
            ).all()
            if value is not None
        ]
        latest_change = max(changed_values) if changed_values else None
        now = utcnow()

        for source in sources:
            source_id = source.source_id
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
                return (
                    False,
                    f"Для источника «{source.name or source_id}» еще нет "
                    "подтвержденной кадровой выгрузки",
                )

            observed_at = self._aware_utc(run.completed_at)
            poll = self.db.scalar(
                select(OneCSourcePollState).where(
                    OneCSourcePollState.source_id == source_id
                )
            )
            if (
                poll is not None
                and poll.last_checked_at is not None
                and str(poll.last_status or "").strip().lower() != "failed"
            ):
                observed_at = max(
                    observed_at,
                    self._aware_utc(poll.last_checked_at),
                )

            if now - observed_at > timedelta(hours=36):
                return (
                    False,
                    f"Источник «{source.name or source_id}» не проверялся "
                    "более 36 часов",
                )
            if latest_change is not None and observed_at < latest_change:
                return (
                    False,
                    f"Ожидается проверка источника «{source.name or source_id}» "
                    "после кадрового изменения. Новый файл не обязателен",
                )

        return True, ""

    def _sources_synchronized(self) -> bool:
        return self._source_confirmation()[0]

    def notice_creation_status(self, candidate: dict) -> dict[str, str]:
        """Объяснить оператору, почему письмо еще не поставлено в очередь."""
        if self.settings.dry_run:
            return {
                "value": "Отключено безопасным режимом",
                "state": "warning",
                "note": "Автоматическая отправка сейчас отключена",
            }
        if self._import_running():
            return {
                "value": "Ожидает завершения импорта",
                "state": "pending",
                "note": "После импорта кадровое состояние будет проверено повторно",
            }
        confirmed, reason = self._source_confirmation()
        if not confirmed:
            return {
                "value": "Ожидает кадровый цикл",
                "state": "pending",
                "note": reason,
            }

        profiles = self._profiles()
        if not profiles:
            return {
                "value": "Не настроен отправитель",
                "state": "error",
                "note": "В настройках почты нет доступного домена отправителя",
            }
        _, recipients = self._event_recipient_plan(candidate, profiles)
        if not recipients:
            organizations = ", ".join(
                str(item.get("source_name") or item.get("source_id") or "").strip()
                for item in candidate.get("organizations") or []
                if str(item.get("source_name") or item.get("source_id") or "").strip()
            )
            return {
                "value": "Нет адреса получателя",
                "state": "error",
                "note": (
                    "Не найден корпоративный email"
                    + (f" для {organizations}" if organizations else "")
                ),
            }

        templates = ensure_dismissal_mail_templates(self.db, self.settings)
        missing_templates = sorted(
            {
                str(item.get("sender_domain") or "").strip().lower()
                for item in recipients
                if str(item.get("sender_domain") or "").strip()
                and (
                    str(item.get("sender_domain") or "").strip().lower()
                    not in templates
                    or not str(
                        templates[
                            str(item.get("sender_domain") or "").strip().lower()
                        ].updated_by
                        or ""
                    ).strip()
                )
            }
        )
        if missing_templates:
            return {
                "value": "Шаблон не подтвержден",
                "state": "warning",
                "note": (
                    "Откройте и сохраните шаблон «Возврат оборудования» для: "
                    + ", ".join(missing_templates)
                ),
            }

        current_events = list(
            self.db.scalars(
                select(HREmploymentDismissalEvent).where(
                    HREmploymentDismissalEvent.worker_key
                    == candidate["worker_key"],
                    HREmploymentDismissalEvent.current_dismissal_date.is_not(None),
                )
            ).all()
        )
        if not current_events:
            return {
                "value": "Ожидает фиксации кадрового события",
                "state": "pending",
                "note": "Событие будет зафиксировано ближайшим фоновым циклом",
            }
        if any(event.noticed_at is not None for event in current_events):
            return {
                "value": "Восстанавливается очередь",
                "state": "warning",
                "note": (
                    "Кадровое событие найдено без связанного письма. "
                    "Очередь будет восстановлена автоматически"
                ),
            }
        return {
            "value": "Ожидает постановки в очередь",
            "state": "pending",
            "note": "Фоновый цикл выполняется один раз в минуту",
        }

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

    def _sync_employment_events(self) -> None:
        """Перенести текущее HR-состояние в устойчивые события организаций."""
        states = list(self.db.scalars(select(HREmploymentState)).all())
        events = list(
            self.db.scalars(
                select(HREmploymentDismissalEvent).order_by(
                    HREmploymentDismissalEvent.sequence
                )
            ).all()
        )
        latest: dict[tuple[str, str], HREmploymentDismissalEvent] = {}
        for event in events:
            latest[(event.worker_key, event.source_id)] = event

        now = utcnow()
        for state in states:
            source_id = str(state.source_id or "").strip().lower()
            key = (state.worker_key, source_id)
            event = latest.get(key)
            dismissal_date = state.dismissal_date

            if dismissal_date is not None:
                if event is None or event.status == "closed":
                    event = HREmploymentDismissalEvent(
                        worker_key=state.worker_key,
                        source_id=source_id,
                        source_name=state.source_name,
                        sequence=(1 if event is None else event.sequence + 1),
                        fio=state.fio,
                        first_dismissal_date=dismissal_date,
                        current_dismissal_date=dismissal_date,
                        status="open",
                    )
                    self.db.add(event)
                    latest[key] = event
                else:
                    # Перенос даты относится к тому же событию.
                    event.current_dismissal_date = dismissal_date
                    event.status = "open"
                    event.source_name = state.source_name
                    event.fio = state.fio
                    event.updated_at = now
                continue

            if event is None or event.status == "closed":
                continue
            if state.is_present:
                # Исчезновение даты не открывает новое событие при ее возврате.
                event.current_dismissal_date = None
                event.status = "cleared"
            else:
                event.status = "absent"
            event.updated_at = now

        self.db.commit()

    @staticmethod
    def _event_ids(notice: DismissalEquipmentNotice) -> list[int]:
        try:
            values = json.loads(notice.event_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return [int(value) for value in values if str(value).isdigit()]

    def _event_candidate(
        self,
        events: list[HREmploymentDismissalEvent],
    ) -> dict:
        dismissal_date = max(
            event.current_dismissal_date or event.first_dismissal_date
            for event in events
        )
        return {
            "worker_key": events[0].worker_key,
            "fio": next((event.fio for event in events if event.fio), ""),
            "dismissal_date": dismissal_date,
            "organizations": [
                {
                    "source_id": event.source_id,
                    "source_name": event.source_name,
                    "dismissal_date": (
                        event.current_dismissal_date or event.first_dismissal_date
                    ),
                }
                for event in events
            ],
        }

    def _event_recipient_plan(self, candidate: dict, profiles: dict) -> tuple[str, list[dict]]:
        """Корпоративные адреса организаций и один личный адрес, если он есть."""
        source_ids = {
            str(item.get("source_id") or "").strip().lower()
            for item in candidate.get("organizations") or []
        }
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key == candidate["worker_key"]
                )
            ).all()
        )
        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == candidate["worker_key"]
                )
            ).all()
        )
        addresses_by_source: dict[str, str] = {}
        for record in records:
            source_id = str(record.source_id or "").strip().lower()
            address = normalize_email(record.corporate_email)
            if source_id in source_ids and address:
                addresses_by_source[source_id] = address
        for mapping in mappings:
            source_id = str(mapping.source_domain or "").strip().lower()
            address = normalize_email(mapping.source_email)
            if source_id in source_ids and address and source_id not in addresses_by_source:
                addresses_by_source[source_id] = address

        recipients: list[dict] = []
        for source_id in sorted(source_ids):
            address = addresses_by_source.get(source_id, "")
            if not address:
                continue
            sender_domain = source_id if source_id in profiles else ""
            if not sender_domain and profiles:
                sender_domain = sorted(profiles)[0]
            if sender_domain:
                recipients.append(
                    {
                        "email": address,
                        "kind": "corporate",
                        "source_id": source_id,
                        "sender_domain": sender_domain,
                        "sent": False,
                        "sent_at": "",
                        "error": "",
                    }
                )
        default_domain = recipients[0]["sender_domain"] if recipients else ""
        corporate_addresses = {
            normalize_email(item.get("email"))
            for item in recipients
            if normalize_email(item.get("email"))
        }
        personal_addresses = sorted(
            {
                normalize_email(record.personal_email)
                for record in records
                if normalize_email(record.personal_email)
                and normalize_email(record.personal_email)
                not in corporate_addresses
            }
        )
        if default_domain:
            for address in personal_addresses:
                recipients.append(
                    {
                        "email": address,
                        "kind": "personal",
                        "source_id": "",
                        "sender_domain": default_domain,
                        "sent": False,
                        "sent_at": "",
                        "error": "",
                    }
                )
        return default_domain, recipients

    @staticmethod
    def _merge_recipients(
        existing: list[dict],
        planned: list[dict],
    ) -> tuple[list[dict], int]:
        """Добавить отсутствующие адреса, не сбрасывая результаты отправки."""
        merged = [dict(item) for item in existing if isinstance(item, dict)]
        known = {
            normalize_email(item.get("email"))
            for item in merged
            if normalize_email(item.get("email"))
        }
        added = 0
        for item in planned:
            address = normalize_email(item.get("email"))
            if not address or address in known:
                continue
            merged.append(dict(item))
            known.add(address)
            added += 1
        return merged, added

    def process(self) -> dict[str, int | str]:
        if self.settings.dry_run:
            return {"status": "dry_run", "created": 0, "sent": 0}
        if self._import_running():
            return {"status": "import_running", "created": 0, "sent": 0}
        if not self._sources_synchronized():
            return {"status": "sources_not_synchronized", "created": 0, "sent": 0}

        self._sync_employment_events()

        profiles = self._profiles()
        dismissal_templates = ensure_dismissal_mail_templates(
            self.db,
            self.settings,
        )
        created = 0
        sent_now = 0

        current_events = list(
            self.db.scalars(
                select(HREmploymentDismissalEvent).where(
                    HREmploymentDismissalEvent.current_dismissal_date.is_not(None),
                )
            ).all()
        )
        existing_notices = list(
            self.db.scalars(select(DismissalEquipmentNotice)).all()
        )
        linked_event_ids = {
            event_id
            for notice in existing_notices
            for event_id in self._event_ids(notice)
        }
        # noticed_at — диагностическая отметка, а не единственный источник
        # истины. Если событие отмечено, но связанного письма в БД нет,
        # следующий цикл автоматически восстановит очередь.
        unnotified = [
            event
            for event in current_events
            if event.noticed_at is None or event.id not in linked_event_ids
        ]
        by_worker: dict[str, list[HREmploymentDismissalEvent]] = defaultdict(list)
        for event in unnotified:
            by_worker[event.worker_key].append(event)

        notices: list[tuple[DismissalEquipmentNotice, dict]] = []
        for events in by_worker.values():
            candidate = self._event_candidate(events)
            notice = None
            if events:
                sender_domain, recipients = self._event_recipient_plan(
                    candidate,
                    profiles,
                )
                if not recipients:
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

                notice = self.db.scalar(
                    select(DismissalEquipmentNotice).where(
                        DismissalEquipmentNotice.worker_key
                        == candidate["worker_key"],
                        DismissalEquipmentNotice.dismissal_date
                        == candidate["dismissal_date"],
                    )
                )
                if notice is None:
                    notice = DismissalEquipmentNotice(
                        worker_key=candidate["worker_key"],
                        dismissal_date=candidate["dismissal_date"],
                        fio=str(candidate.get("fio") or ""),
                        sender_domain=sender_domain,
                        event_ids_json=json.dumps([event.id for event in events]),
                        recipients_json=json.dumps(
                            recipients,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        status="pending",
                    )
                    self.db.add(notice)
                    created += 1
                else:
                    combined_ids = list(
                        dict.fromkeys(
                            [*self._event_ids(notice), *[event.id for event in events]]
                        )
                    )
                    combined_events = list(
                        self.db.scalars(
                            select(HREmploymentDismissalEvent).where(
                                HREmploymentDismissalEvent.id.in_(combined_ids)
                            )
                        ).all()
                    )
                    combined_candidate = self._event_candidate(combined_events)
                    _, combined_plan = self._event_recipient_plan(
                        combined_candidate,
                        profiles,
                    )
                    merged, added = self._merge_recipients(
                        self._load_recipients(notice),
                        combined_plan,
                    )
                    notice.event_ids_json = json.dumps(combined_ids)
                    notice.recipients_json = json.dumps(
                        merged,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    notice.fio = str(combined_candidate.get("fio") or notice.fio)
                    candidate = combined_candidate
                    if added:
                        sent_count = sum(
                            1 for item in merged if item.get("sent")
                        )
                        notice.status = "partial" if sent_count else "pending"
                        notice.next_attempt_at = None
                        notice.last_error = ""
                    notice.updated_at = utcnow()
                for event in events:
                    event.noticed_at = utcnow()
                self.db.commit()
                self.db.refresh(notice)
                notices.append((notice, candidate))

        retry_notices = list(
            self.db.scalars(
                select(DismissalEquipmentNotice).where(
                    DismissalEquipmentNotice.status.in_(["pending", "partial", "failed"])
                )
            ).all()
        )
        queued_ids = {notice.id for notice, _ in notices}
        for notice in retry_notices:
            if notice.id in queued_ids:
                continue
            ids = self._event_ids(notice)
            events = list(
                self.db.scalars(
                    select(HREmploymentDismissalEvent).where(
                        HREmploymentDismissalEvent.id.in_(ids)
                    )
                ).all()
            ) if ids else []
            if events:
                notices.append((notice, self._event_candidate(events)))

        for notice, candidate in notices:
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
            "candidates": len(unnotified),
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
        # process() сам пропускает цикл, пока идет кадровый импорт, поэтому
        # после короткой форы стартовому IMAP-опросу можно восстановить
        # накопившуюся очередь, не ожидая полную минуту.
        if self._stop_event.wait(5):
            return
        self._run_once()
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
