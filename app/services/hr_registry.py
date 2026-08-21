from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    DomainAccessUser,
    EmailLoginMapping,
    HRPerson,
    HRSourceRecord,
)
from app.models_onec_sources import HREmploymentState
from app.services.ad import ActiveDirectoryService
from app.services.email_login_mapping import EmailLoginMappingService
from app.services.hr_employment import sync_workbook_employment
from app.services.employee_arrivals import (
    begin_arrival_source_sync,
    sync_employment_arrival,
)
from app.services.onec_xlsx import OneCWorkbook
from app.services.zimbra import ZimbraService


AD_LABELS = {
    "enabled": "Есть, включена",
    "disabled": "Есть, отключена",
    "missing": "Не найдена",
    "error": "Ошибка проверки",
    "not_checked": "Не проверено",
    "no_login": "Нет логина",
}
ZIMBRA_LABELS = {
    "active": "Есть, активна",
    "closed": "Есть, закрыта",
    "locked": "Есть, заблокирована",
    "lockout": "Есть, вход заблокирован",
    "maintenance": "Есть, обслуживание",
    "pending": "Есть, ожидает активации",
    # Совместимость со старыми строками до первой новой сверки.
    "present": "Есть, статус не уточнен",
    "missing": "Адрес не найден",
    "address_mismatch": "Ящик найден, e-mail 1С не привязан",
    "error": "Ошибка проверки",
    "not_checked": "Не проверено",
    "no_email": "Нет e-mail в 1С",
}
RECON_LABELS = {
    "ok": "Соответствует",
    "checked": "Проверен",
    "issue": "Требует проверки",
    "error": "Ошибка сверки",
    "not_checked": "Не проверено полностью",
}

ZIMBRA_ACCOUNT_STATES = {
    "active",
    "closed",
    "locked",
    "lockout",
    "maintenance",
    "pending",
}


def zimbra_registry_status(identity) -> str:
    """Нормализовать реальный zimbraAccountStatus для кадрового реестра."""
    if identity is None:
        return "missing"
    status = str(getattr(identity, "account_status", "") or "").strip().lower()
    return status if status in ZIMBRA_ACCOUNT_STATES else "present"


def reconciliation_status_for(
    record: HRSourceRecord,
    *,
    requires_active_accounts: bool | None,
) -> str:
    """Сверить фактическое состояние УЗ с кадровым состоянием человека.

    requires_active_accounts=True  – человек продолжает работать хотя бы в
    одной организации; False – окончательно уволен во всех организациях;
    None – старые/неполные кадровые данные, сохраняем прежнюю модель ожиданий.
    """
    if record.ad_status == "error" or record.zimbra_status == "error":
        return "error"

    expect_active = requires_active_accounts is not False
    if expect_active:
        ad_ok = record.ad_status == "enabled"
        # present оставлен только как совместимость до первой новой сверки.
        zimbra_ok = record.zimbra_status in {"active", "present"}
        ad_unknown = record.ad_status == "not_checked"
        zimbra_unknown = record.zimbra_status == "not_checked"
    else:
        ad_ok = record.ad_status in {"disabled", "missing", "no_login"}
        zimbra_ok = record.zimbra_status in {"closed", "missing", "no_email"}
        ad_unknown = record.ad_status == "not_checked"
        zimbra_unknown = record.zimbra_status == "not_checked"

    # Известное несоответствие важнее второго непроверенного источника.
    if (not ad_ok and not ad_unknown) or (not zimbra_ok and not zimbra_unknown):
        return "issue"
    if ad_unknown or zimbra_unknown:
        return "not_checked"
    return "ok" if ad_ok and zimbra_ok else "issue"


def worker_requires_active_accounts(
    states: list[HREmploymentState],
) -> bool | None:
    statuses = {str(state.status or "").strip().lower() for state in states}
    statuses.discard("")
    if statuses & {"active", "scheduled"}:
        return True
    if statuses and statuses <= {"dismissed"}:
        return False
    return None


MANUAL_CHECK_ACTION = "hr_accounts_not_required_confirmed"
MANUAL_CHECK_INVALIDATED_ACTION = "hr_accounts_not_required_invalidated"
MANUAL_CHECK_ACTIONS = {
    MANUAL_CHECK_ACTION,
    MANUAL_CHECK_INVALIDATED_ACTION,
}
ABSENT_AD_STATUSES = {"missing", "no_login"}
ABSENT_ZIMBRA_STATUSES = {"missing", "no_email"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HRRegistryService:
    """Кадровый реестр и read-only сверка 1С ↔ AD ↔ Zimbra."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self.mapping_service = EmailLoginMappingService(settings, db)
        try:
            self.source_id = self.mapping_service.resolve_source_domain()
        except ValueError:
            # Before the first import with the new patch the existing rows may
            # still carry the legacy placeholder.
            self.source_id = "org_com"
        self.source_name = (
            self.source_id
            if self.source_id != "org_com"
            else "Домен еще не определен"
        )

    @staticmethod
    def _check_target(record: HRSourceRecord) -> str:
        return f"{record.source_id}:{record.worker_key}"

    @staticmethod
    def _json_details(event: AuditLog | None) -> dict:
        if event is None or not event.details:
            return {}
        try:
            value = json.loads(event.details)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _short_operator_name(display_name: str, username: str) -> str:
        value = " ".join(str(display_name or "").split())
        parts = value.split(" ") if value else []
        if len(parts) >= 2:
            initials = "".join(
                f"{part[0].upper()}."
                for part in parts[1:3]
                if part
            )
            if initials:
                return f"{parts[0]} {initials}"
        return value or username

    def _operator_names(
        self,
        username: str,
        source: str,
    ) -> tuple[str, str]:
        normalized = str(username or "").strip().lower()
        display_name = normalized
        if source == "ad" and normalized:
            account = self.db.scalar(
                select(DomainAccessUser).where(
                    DomainAccessUser.username == normalized
                )
            )
            if account is not None and account.display_name.strip():
                display_name = account.display_name.strip()
        return (
            self._short_operator_name(display_name, normalized),
            display_name,
        )

    def _latest_manual_check_events(
        self,
        records: list[HRSourceRecord],
    ) -> tuple[dict[str, AuditLog], dict[str, AuditLog]]:
        if not records:
            return {}, {}
        targets = {self._check_target(record) for record in records}
        events = self.db.scalars(
            select(AuditLog)
            .where(AuditLog.action.in_(MANUAL_CHECK_ACTIONS))
            .order_by(AuditLog.id.desc())
        ).all()
        latest: dict[str, AuditLog] = {}
        latest_confirmation: dict[str, AuditLog] = {}
        for event in events:
            if event.target not in targets:
                continue
            latest.setdefault(event.target, event)
            if event.action == MANUAL_CHECK_ACTION:
                latest_confirmation.setdefault(event.target, event)
            if (
                len(latest) == len(targets)
                and len(latest_confirmation) == len(targets)
            ):
                break
        return latest, latest_confirmation

    @staticmethod
    def _snapshot_matches(record: HRSourceRecord, payload: dict) -> bool:
        return bool(
            str(payload.get("placements_json") or "")
            == str(record.placements_json or "")
            and str(payload.get("corporate_email") or "").strip().lower()
            == record.corporate_email.strip().lower()
            and str(payload.get("login") or "").strip().lower()
            == record.login.strip().lower()
        )

    @staticmethod
    def _accounts_are_absent(record: HRSourceRecord) -> bool:
        return bool(
            record.ad_status in ABSENT_AD_STATUSES
            and record.zimbra_status in ABSENT_ZIMBRA_STATUSES
        )

    def _manual_check_state(
        self,
        record: HRSourceRecord,
        latest_event: AuditLog | None,
        latest_confirmation: AuditLog | None,
    ) -> dict:
        if latest_confirmation is None:
            return {
                "active": False,
                "operator": "",
                "operator_name": "",
                "confirmed_at": None,
                "note": "",
                "meta": "",
                "previous_note": "",
            }

        confirmation = self._json_details(latest_confirmation)
        operator = str(
            confirmation.get("operator_username")
            or latest_confirmation.actor
            or ""
        ).strip()
        operator_name = str(
            confirmation.get("operator_short_name")
            or confirmation.get("operator_display_name")
            or operator
        ).strip()
        note = (
            f"{operator_name} подтвердил, что учетные записи не требуются"
            if operator_name
            else "Оператор подтвердил, что учетные записи не требуются"
        )

        invalidation_reason = ""
        invalidated = bool(
            latest_event is not None
            and latest_event.action == MANUAL_CHECK_INVALIDATED_ACTION
            and latest_event.id > latest_confirmation.id
        )
        if invalidated:
            invalidation_reason = str(
                self._json_details(latest_event).get("reason") or ""
            ).strip()

        active = bool(
            not invalidated
            and self._snapshot_matches(record, confirmation)
            and self._accounts_are_absent(record)
        )

        if not active and not invalidation_reason:
            if not self._snapshot_matches(record, confirmation):
                invalidation_reason = "Изменились кадровые или учетные данные"
            elif not self._accounts_are_absent(record):
                invalidation_reason = "Обнаружены учетные записи"

        previous_note = ""
        if not active and note:
            previous_note = f"Ранее {note}."
            if invalidation_reason:
                previous_note += f" {invalidation_reason}."

        return {
            "active": active,
            "operator": operator,
            "operator_name": operator_name,
            "confirmed_at": latest_confirmation.created_at,
            "note": note if active else "",
            "meta": operator if active else "",
            "previous_note": previous_note,
        }

    def _manual_check_states(
        self,
        records: list[HRSourceRecord],
    ) -> dict[int, dict]:
        latest, confirmations = self._latest_manual_check_events(records)
        return {
            record.id: self._manual_check_state(
                record,
                latest.get(self._check_target(record)),
                confirmations.get(self._check_target(record)),
            )
            for record in records
        }

    def _invalidate_manual_check(
        self,
        record: HRSourceRecord,
        confirmation: AuditLog,
        reason: str,
    ) -> AuditLog:
        payload = self._json_details(confirmation)
        event = AuditLog(
            actor="system",
            action=MANUAL_CHECK_INVALIDATED_ACTION,
            target=self._check_target(record),
            result="invalidated",
            details=json.dumps(
                {
                    "version": 1,
                    "reason": reason,
                    "previous_operator_username": (
                        payload.get("operator_username")
                        or confirmation.actor
                    ),
                    "previous_operator_short_name": (
                        payload.get("operator_short_name") or ""
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        self.db.add(event)
        return event

    def mark_accounts_not_required(
        self,
        record_id: int,
        operator_username: str,
        operator_source: str,
    ) -> dict:
        record = self.db.get(HRSourceRecord, record_id)
        if record is None or not record.is_present:
            raise LookupError("Работник не найден в текущем кадровом реестре")
        if record.reconciled_at is None:
            raise ValueError("Сначала выполните сверку 1С / AD / Zimbra")
        if record.reconciliation_status != "issue":
            raise ValueError("Для этой записи подтверждение не требуется")
        if not self._accounts_are_absent(record):
            raise ValueError(
                "Статус «Проверен» доступен только когда учетные записи AD и Zimbra не обнаружены"
            )

        latest, confirmations = self._latest_manual_check_events([record])
        state = self._manual_check_state(
            record,
            latest.get(self._check_target(record)),
            confirmations.get(self._check_target(record)),
        )
        if state.get("active"):
            return {
                "operator": state.get("operator", ""),
                "operator_name": state.get("operator_name", ""),
                "confirmed_at": state.get("confirmed_at"),
            }

        operator_short_name, operator_display_name = self._operator_names(
            operator_username,
            operator_source,
        )
        payload = {
            "version": 1,
            "worker_key": record.worker_key,
            "source_id": record.source_id,
            "operator_username": operator_username,
            "operator_source": operator_source,
            "operator_short_name": operator_short_name,
            "operator_display_name": operator_display_name,
            "fio": record.fio,
            "placements_json": record.placements_json or "[]",
            "corporate_email": record.corporate_email,
            "login": record.login,
            "ad_status": record.ad_status,
            "zimbra_status": record.zimbra_status,
            "reconciled_at": (
                record.reconciled_at.isoformat()
                if record.reconciled_at is not None
                else ""
            ),
        }
        event = AuditLog(
            actor=operator_username,
            action=MANUAL_CHECK_ACTION,
            target=self._check_target(record),
            result="confirmed",
            details=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return {
            "operator": operator_username,
            "operator_name": operator_short_name,
            "confirmed_at": event.created_at,
        }

    def _set_source_from_workbook(self, workbook: OneCWorkbook) -> str:
        domain = self.mapping_service.infer_source_domain_from_workbook(
            workbook
        )
        self.source_id = domain
        self.source_name = domain
        return domain

    def sync_workbook(self, workbook: OneCWorkbook) -> dict[str, int]:
        source_id = self._set_source_from_workbook(workbook)
        now = utcnow()
        current_keys = {worker.worker_key for worker in workbook.workers}

        source_records = self.db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == source_id
            )
        ).all()
        arrival_baseline = begin_arrival_source_sync(
            self.db,
            source_id=source_id,
            source_name=self.source_name,
            has_existing_records=bool(source_records),
        )
        existing_records = {
            record.worker_key: record
            for record in source_records
        }
        latest_checks, _ = self._latest_manual_check_events(source_records)

        people = (
            self.db.scalars(
                select(HRPerson).where(
                    HRPerson.worker_key.in_(current_keys)
                )
            ).all()
            if current_keys
            else []
        )
        people_by_key = {
            person.worker_key: person
            for person in people
        }

        created_people = 0
        created_source_records = 0

        for worker in workbook.workers:
            person = people_by_key.get(worker.worker_key)
            is_new_person = person is None
            if person is None:
                person = HRPerson(
                    worker_key=worker.worker_key,
                    fio=worker.fio,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                self.db.add(person)
                people_by_key[worker.worker_key] = person
                created_people += 1
            else:
                person.fio = worker.fio
                person.last_seen_at = now

            placements_json = json.dumps(
                [
                    {
                        "department": placement.department or "",
                        "position": placement.position or "",
                    }
                    for placement in worker.placements
                ],
                ensure_ascii=False,
                sort_keys=True,
            )

            record = existing_records.get(worker.worker_key)
            episode_started = record is None or not record.is_present
            if record is None:
                record = HRSourceRecord(
                    worker_key=worker.worker_key,
                    source_id=source_id,
                    source_name=source_id,
                    fio=worker.fio,
                    corporate_email=worker.email or "",
                    personal_email=worker.personal_email or "",
                    mobile_phone=worker.mobile_phone or "",
                    login=worker.login or "",
                    placements_json=placements_json,
                    is_present=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                self.db.add(record)
                existing_records[worker.worker_key] = record
                created_source_records += 1
            else:
                latest_event = latest_checks.get(self._check_target(record))
                if (
                    latest_event is not None
                    and latest_event.action == MANUAL_CHECK_ACTION
                ):
                    payload = self._json_details(latest_event)
                    reason = ""
                    if not record.is_present:
                        reason = "Работник повторно появился в кадровой выгрузке"
                    elif str(payload.get("placements_json") or "") != placements_json:
                        reason = "Изменилось кадровое назначение"
                    elif (
                        str(payload.get("corporate_email") or "").strip().lower()
                        != str(worker.email or "").strip().lower()
                    ):
                        reason = "Изменился корпоративный e-mail"
                    elif (
                        str(payload.get("login") or "").strip().lower()
                        != str(worker.login or "").strip().lower()
                    ):
                        reason = "Изменился логин из кадровой выгрузки"
                    if reason:
                        self._invalidate_manual_check(
                            record,
                            latest_event,
                            reason,
                        )

                record.source_name = source_id
                record.fio = worker.fio
                record.corporate_email = worker.email or ""
                record.personal_email = worker.personal_email or ""
                record.mobile_phone = worker.mobile_phone or ""
                record.login = worker.login or ""
                record.placements_json = placements_json
                record.is_present = True
                record.last_seen_at = now

            sync_employment_arrival(
                self.db,
                worker_key=worker.worker_key,
                source_id=source_id,
                source_name=self.source_name,
                fio=worker.fio,
                is_present=True,
                episode_started=episode_started,
                is_new_person=is_new_person,
                baseline=arrival_baseline,
                seen_at=now,
            )

        missing = 0
        for key, record in existing_records.items():
            if key not in current_keys and record.is_present:
                record.is_present = False
                sync_employment_arrival(
                    self.db,
                    worker_key=record.worker_key,
                    source_id=source_id,
                    source_name=self.source_name,
                    fio=record.fio,
                    is_present=False,
                    episode_started=False,
                    seen_at=now,
                )
                missing += 1

        employment = sync_workbook_employment(
            self.db,
            workbook=workbook,
            source_id=source_id,
            source_name=self.source_name,
            timezone_name=getattr(self.settings, "app_timezone", "UTC"),
        )

        self.db.commit()
        return {
            "created_people": created_people,
            "created_source_records": created_source_records,
            "marked_missing": missing,
            **employment,
        }

    def reconcile_current(self) -> dict[str, int | str]:
        try:
            source_id = self.mapping_service.resolve_source_domain()
            self.source_id = source_id
            self.source_name = source_id
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        self.mapping_service.cleanup_dismissed(source_id)

        records = self.db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == source_id,
                HRSourceRecord.is_present.is_(True),
            )
        ).all()
        latest_checks, _ = self._latest_manual_check_events(records)

        worker_keys = [record.worker_key for record in records]
        employment_rows = (
            list(
                self.db.scalars(
                    select(HREmploymentState).where(
                        HREmploymentState.worker_key.in_(worker_keys)
                    )
                ).all()
            )
            if worker_keys
            else []
        )
        employment_by_worker: dict[str, list[HREmploymentState]] = {}
        for employment in employment_rows:
            employment_by_worker.setdefault(
                employment.worker_key,
                [],
            ).append(employment)
        requires_active_by_worker = {
            worker_key: worker_requires_active_accounts(
                employment_by_worker.get(worker_key, [])
            )
            for worker_key in worker_keys
        }

        mappings = self.mapping_service.mapping_by_worker(
            worker_keys,
            source_id,
        )

        ad = ActiveDirectoryService(self.settings)
        zimbra = ZimbraService(self.settings)

        mapped_ad = {}
        mapped_zimbra = {}
        mapped_ad_error = ""
        mapped_zimbra_error = ""

        if mappings and self.settings.ad_check_enabled:
            try:
                mapped_ad = ad.users_by_object_guids(
                    [
                        mapping.ad_object_guid
                        for mapping in mappings.values()
                    ]
                )
            except Exception as exc:
                mapped_ad_error = str(exc)

        if mappings and self.settings.zimbra_check_enabled:
            try:
                mapped_zimbra = zimbra.accounts_by_ids(
                    [
                        mapping.zimbra_id
                        for mapping in mappings.values()
                    ]
                )
            except Exception as exc:
                mapped_zimbra_error = str(exc)

        # Unmapped rows resolve the actual Zimbra account first. This means an
        # alias in 1C can still lead to the real Zimbra login, which is then
        # checked in AD.
        unmapped_emails = sorted(
            {
                record.corporate_email.strip().lower()
                for record in records
                if record.worker_key not in mappings
                and record.corporate_email.strip()
            }
        )
        auto_zimbra = {}
        auto_zimbra_error = ""
        if self.settings.zimbra_check_enabled and unmapped_emails:
            try:
                auto_zimbra = zimbra.accounts_by_addresses(
                    unmapped_emails
                )
            except Exception as exc:
                auto_zimbra_error = str(exc)

        auto_ad_logins: set[str] = set()
        candidate_logins: dict[str, str] = {}

        for record in records:
            if record.worker_key in mappings:
                continue
            email = record.corporate_email.strip().lower()
            z_identity = auto_zimbra.get(email)
            if z_identity is not None:
                candidate = z_identity.login
            else:
                candidate = record.login.strip().lower()
            candidate_logins[record.worker_key] = candidate
            if candidate:
                auto_ad_logins.add(candidate)

        auto_ad = {}
        auto_ad_error = ""
        if self.settings.ad_check_enabled and auto_ad_logins:
            try:
                auto_ad = ad.users_by_logins(
                    sorted(auto_ad_logins)
                )
            except Exception as exc:
                auto_ad_error = str(exc)

        now = utcnow()
        redundant_mapping_ids: list[int] = []

        for record in records:
            errors: list[str] = []
            email = record.corporate_email.strip().lower()
            mapping = mappings.get(record.worker_key)

            if mapping is not None:
                ad_user = mapped_ad.get(
                    mapping.ad_object_guid.strip().lower()
                )
                z_identity = mapped_zimbra.get(
                    mapping.zimbra_id.strip()
                )

                if not self.settings.ad_check_enabled:
                    record.ad_status = "not_checked"
                elif mapped_ad_error:
                    record.ad_status = "error"
                    errors.append(f"AD: {mapped_ad_error}")
                elif ad_user is None:
                    record.ad_status = "missing"
                else:
                    record.ad_status = (
                        "enabled" if ad_user.is_enabled else "disabled"
                    )

                if not email:
                    record.zimbra_status = "no_email"
                elif not self.settings.zimbra_check_enabled:
                    record.zimbra_status = "not_checked"
                elif mapped_zimbra_error:
                    record.zimbra_status = "error"
                    errors.append(
                        f"Zimbra: {mapped_zimbra_error}"
                    )
                elif z_identity is None:
                    record.zimbra_status = "missing"
                elif email not in z_identity.addresses:
                    record.zimbra_status = "address_mismatch"
                else:
                    record.zimbra_status = zimbra_registry_status(
                        z_identity
                    )

                if ad_user is not None and z_identity is not None:
                    mapping.ad_login = ad_user.username
                    mapping.zimbra_email = z_identity.primary_email
                    mapping.last_verified_at = now

                    # The exception has healed: both systems now use the same
                    # real login. The explicit mapping is no longer needed.
                    if (
                        ad_user.username.lower()
                        == z_identity.login.lower()
                    ):
                        redundant_mapping_ids.append(mapping.id)

            else:
                z_identity = auto_zimbra.get(email)

                if not email:
                    record.zimbra_status = "no_email"
                elif not self.settings.zimbra_check_enabled:
                    record.zimbra_status = "not_checked"
                elif auto_zimbra_error:
                    record.zimbra_status = "error"
                    errors.append(
                        f"Zimbra: {auto_zimbra_error}"
                    )
                elif z_identity is None:
                    record.zimbra_status = "missing"
                else:
                    record.zimbra_status = zimbra_registry_status(
                        z_identity
                    )

                candidate = candidate_logins.get(
                    record.worker_key,
                    "",
                )
                if not candidate:
                    record.ad_status = "no_login"
                elif not self.settings.ad_check_enabled:
                    record.ad_status = "not_checked"
                elif auto_ad_error:
                    record.ad_status = "error"
                    errors.append(f"AD: {auto_ad_error}")
                else:
                    ad_user = auto_ad.get(candidate)
                    if ad_user is None:
                        record.ad_status = "missing"
                    else:
                        record.ad_status = (
                            "enabled"
                            if ad_user.is_enabled
                            else "disabled"
                        )

            latest_event = latest_checks.get(self._check_target(record))
            if (
                latest_event is not None
                and latest_event.action == MANUAL_CHECK_ACTION
                and not self._accounts_are_absent(record)
                and record.ad_status != "error"
                and record.zimbra_status != "error"
            ):
                if record.ad_status not in ABSENT_AD_STATUSES:
                    reason = "Обнаружена учетная запись Active Directory"
                elif record.zimbra_status not in ABSENT_ZIMBRA_STATUSES:
                    reason = "Обнаружена учетная запись Zimbra"
                else:
                    reason = "Изменилось состояние учетных записей"
                self._invalidate_manual_check(
                    record,
                    latest_event,
                    reason,
                )

            record.reconciliation_status = reconciliation_status_for(
                record,
                requires_active_accounts=requires_active_by_worker.get(
                    record.worker_key
                ),
            )

            record.reconciliation_error = "\n".join(errors)
            record.reconciled_at = now

        if redundant_mapping_ids:
            redundant = self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.id.in_(
                        redundant_mapping_ids
                    )
                )
            ).all()
            for mapping in redundant:
                self.db.delete(mapping)

        self.db.commit()
        return self.summary()

    def sync_and_reconcile(self, workbook: OneCWorkbook) -> dict:
        return {
            "sync": self.sync_workbook(workbook),
            "reconciliation": self.reconcile_current(),
        }

    def summary(self) -> dict[str, int | str]:
        try:
            source_id = self.mapping_service.resolve_source_domain()
            self.source_id = source_id
            self.source_name = source_id
        except ValueError:
            source_id = self.source_id

        records = self.db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == source_id,
                HRSourceRecord.is_present.is_(True),
            )
        ).all()
        manual_states = self._manual_check_states(records)

        # Count in Python because the mapping table is intentionally small.
        mapping_count = len(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.source_domain == source_id
                )
            ).all()
        )

        summary: dict[str, int | str] = {
            "source_id": source_id,
            "source_name": self.source_name,
            "total": len(records),
            "ok": 0,
            "checked": 0,
            "issues": 0,
            "errors": 0,
            "not_checked": 0,
            "ad_missing": 0,
            "ad_disabled": 0,
            "zimbra_missing": 0,
            "no_email": 0,
            "mapping_count": mapping_count,
        }
        for record in records:
            manual_state = manual_states.get(record.id, {})
            if manual_state.get("active"):
                summary["checked"] += 1
            elif record.reconciliation_status == "ok":
                summary["ok"] += 1
            elif record.reconciliation_status == "issue":
                summary["issues"] += 1
            elif record.reconciliation_status == "error":
                summary["errors"] += 1
            else:
                summary["not_checked"] += 1

            if record.ad_status == "missing":
                summary["ad_missing"] += 1
            elif record.ad_status == "disabled":
                summary["ad_disabled"] += 1
            if record.zimbra_status == "missing":
                summary["zimbra_missing"] += 1
            elif record.zimbra_status == "no_email":
                summary["no_email"] += 1
        return summary

    def list_rows(
        self,
        *,
        query: str = "",
        status: str = "all",
        limit: int = 1000,
    ) -> list[dict]:
        try:
            source_id = self.mapping_service.resolve_source_domain()
            self.source_id = source_id
            self.source_name = source_id
        except ValueError:
            source_id = self.source_id

        records = self.db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == source_id,
                HRSourceRecord.is_present.is_(True),
            ).order_by(HRSourceRecord.fio)
        ).all()
        manual_states = self._manual_check_states(records)

        mappings = self.mapping_service.mapping_by_worker(
            [record.worker_key for record in records],
            source_id,
        )

        q = query.strip().casefold()
        rows: list[dict] = []
        for record in records:
            mapping = mappings.get(record.worker_key)
            effective_login = (
                mapping.ad_login.strip().lower()
                if mapping is not None and mapping.ad_login.strip()
                else record.login
            )

            if q and q not in " ".join(
                [
                    record.fio,
                    record.login,
                    effective_login,
                    record.corporate_email,
                    record.personal_email,
                ]
            ).casefold():
                continue

            manual_state = manual_states.get(record.id, {})
            is_checked = bool(manual_state.get("active"))
            effective_status = (
                "checked" if is_checked else record.reconciliation_status
            )

            if (
                status == "issues"
                and effective_status not in {"issue", "error"}
            ):
                continue
            if status == "ok" and effective_status != "ok":
                continue
            if status == "checked" and effective_status != "checked":
                continue
            if (
                status == "not_checked"
                and effective_status != "not_checked"
            ):
                continue

            try:
                placements = json.loads(
                    record.placements_json or "[]"
                )
            except json.JSONDecodeError:
                placements = []

            placement_labels = []
            for placement in placements:
                department = str(
                    placement.get("department") or ""
                ).strip()
                position = str(
                    placement.get("position") or ""
                ).strip()
                label = " / ".join(
                    part
                    for part in [department, position]
                    if part
                )
                if label:
                    placement_labels.append(label)

            create_url = ""
            if (
                record.ad_status in ABSENT_AD_STATUSES
                and record.zimbra_status in ABSENT_ZIMBRA_STATUSES
            ):
                raw_input = record.fio.strip()
                if record.personal_email.strip():
                    raw_input += "\n" + record.personal_email.strip()
                create_url = "/employees/new?" + urlencode(
                    {"fio": raw_input}
                )

            create_ad_url = ""
            if (
                mapping is None
                and record.ad_status == "missing"
                and record.zimbra_status in {"active", "present"}
                and record.corporate_email.strip()
            ):
                create_ad_url = (
                    f"/employees/registry/{record.id}/create-ad"
                )

            mapping_url = ""
            if (
                effective_status in {"issue", "error"}
                and record.corporate_email
            ):
                mapping_url = (
                    "/settings/email-login-mapping?"
                    + urlencode(
                        {
                            "email": record.corporate_email,
                        }
                    )
                )

            can_mark_checked = bool(
                effective_status == "issue"
                and self._accounts_are_absent(record)
            )

            rows.append(
                {
                    "id": record.id,
                    "fio": record.fio,
                    "source_name": record.source_name,
                    "email": record.corporate_email,
                    "personal_email": record.personal_email,
                    # В реестре показываем фактический логин AD из явного
                    # сопоставления. Логин, вычисленный из e-mail 1С,
                    # остается только исходным кандидатом.
                    "login": effective_login,
                    "source_login": record.login,
                    "login_from_mapping": mapping is not None,
                    "placements": placement_labels,
                    "ad_status": record.ad_status,
                    "ad_label": AD_LABELS.get(
                        record.ad_status,
                        record.ad_status,
                    ),
                    "zimbra_status": record.zimbra_status,
                    "zimbra_label": ZIMBRA_LABELS.get(
                        record.zimbra_status,
                        record.zimbra_status,
                    ),
                    "reconciliation_status": effective_status,
                    "reconciliation_class": (
                        "ok" if effective_status == "checked" else effective_status
                    ),
                    "reconciliation_label": RECON_LABELS.get(
                        effective_status,
                        effective_status,
                    ),
                    "error": record.reconciliation_error,
                    "reconciled_at": record.reconciled_at,
                    "create_url": create_url,
                    "create_ad_url": create_ad_url,
                    "mapping_url": mapping_url,
                    "has_mapping": mapping is not None,
                    "can_mark_checked": can_mark_checked,
                    "manual_check_note": manual_state.get("note", ""),
                    "manual_check_operator": manual_state.get("operator", ""),
                    "manual_check_confirmed_at": manual_state.get(
                        "confirmed_at"
                    ),
                    "manual_check_previous_note": manual_state.get(
                        "previous_note", ""
                    ),
                }
            )
            if len(rows) >= limit:
                break
        return rows
