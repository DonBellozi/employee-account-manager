from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EmailLoginMapping, HRPerson, HRSourceRecord
from app.services.ad import ActiveDirectoryService
from app.services.email_login_mapping import (
    EmailLoginMappingService,
    email_domain,
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
    "present": "Адрес существует",
    "missing": "Адрес не найден",
    "address_mismatch": "Ящик найден, e-mail 1С не привязан",
    "error": "Ошибка проверки",
    "not_checked": "Не проверено",
    "no_email": "Нет e-mail в 1С",
}
RECON_LABELS = {
    "ok": "Соответствует",
    "issue": "Требует проверки",
    "error": "Ошибка сверки",
    "not_checked": "Не проверено полностью",
}


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
        existing_records = {
            record.worker_key: record
            for record in source_records
        }

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
            if record is None:
                record = HRSourceRecord(
                    worker_key=worker.worker_key,
                    source_id=source_id,
                    source_name=source_id,
                    fio=worker.fio,
                    corporate_email=worker.email or "",
                    personal_email=worker.personal_email or "",
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
                record.source_name = source_id
                record.fio = worker.fio
                record.corporate_email = worker.email or ""
                record.personal_email = worker.personal_email or ""
                record.login = worker.login or ""
                record.placements_json = placements_json
                record.is_present = True
                record.last_seen_at = now

        missing = 0
        for key, record in existing_records.items():
            if key not in current_keys and record.is_present:
                record.is_present = False
                missing += 1

        self.db.commit()
        return {
            "created_people": created_people,
            "created_source_records": created_source_records,
            "marked_missing": missing,
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

        worker_keys = [record.worker_key for record in records]
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
                    record.zimbra_status = "present"

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
                    record.zimbra_status = "present"

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

            if (
                record.ad_status == "error"
                or record.zimbra_status == "error"
            ):
                record.reconciliation_status = "error"
            elif (
                record.ad_status
                in {"missing", "disabled", "no_login"}
                or record.zimbra_status
                in {
                    "missing",
                    "no_email",
                    "address_mismatch",
                }
            ):
                record.reconciliation_status = "issue"
            elif (
                record.ad_status == "not_checked"
                or record.zimbra_status == "not_checked"
            ):
                record.reconciliation_status = "not_checked"
            else:
                record.reconciliation_status = "ok"

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
            if record.reconciliation_status == "ok":
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

            if (
                status == "issues"
                and record.reconciliation_status
                not in {"issue", "error"}
            ):
                continue
            if (
                status == "ok"
                and record.reconciliation_status != "ok"
            ):
                continue
            if (
                status == "not_checked"
                and record.reconciliation_status
                != "not_checked"
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
                record.ad_status == "missing"
                and record.zimbra_status == "missing"
            ):
                create_url = "/employees/new?" + urlencode(
                    {"fio": record.fio}
                )

            create_ad_url = ""
            if (
                mapping is None
                and record.ad_status == "missing"
                and record.zimbra_status == "present"
                and record.corporate_email.strip()
            ):
                create_ad_url = (
                    f"/employees/registry/{record.id}/create-ad"
                )

            mapping_url = ""
            if (
                record.reconciliation_status in {"issue", "error"}
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
                    "reconciliation_status": (
                        record.reconciliation_status
                    ),
                    "reconciliation_label": RECON_LABELS.get(
                        record.reconciliation_status,
                        record.reconciliation_status,
                    ),
                    "error": record.reconciliation_error,
                    "reconciled_at": record.reconciled_at,
                    "create_url": create_url,
                    "create_ad_url": create_ad_url,
                    "mapping_url": mapping_url,
                    "has_mapping": mapping is not None,
                }
            )
            if len(rows) >= limit:
                break
        return rows
