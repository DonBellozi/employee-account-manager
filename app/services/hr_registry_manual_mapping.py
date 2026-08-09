from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.services.ad import ADDirectoryUser, ActiveDirectoryService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


class HRRegistryManualMappingService:
    """Ручное сопоставление конкретной кадровой записи с AD/Zimbra."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def get_record(self, record_id: int) -> HRSourceRecord:
        record = self.db.get(HRSourceRecord, int(record_id))
        if record is None or not record.is_present:
            raise LookupError("Работник не найден в текущем кадровом реестре")
        return record

    def get_mapping(
        self,
        record: HRSourceRecord,
    ) -> EmailLoginMapping | None:
        return self.db.scalar(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key == record.worker_key,
                EmailLoginMapping.source_domain
                == str(record.source_id or "").strip().lower(),
            )
        )

    def page_data(self, record_id: int) -> dict:
        record = self.get_record(record_id)
        mapping = self.get_mapping(record)
        return {
            "record_id": record.id,
            "fio": record.fio,
            "source_id": record.source_id,
            "source_name": record.source_name or record.source_id,
            "source_email": record.corporate_email,
            "source_login": record.login,
            "mapped_email": mapping.source_email if mapping else "",
            "mapped_ad_login": mapping.ad_login if mapping else "",
            "mapped_zimbra_email": mapping.zimbra_email if mapping else "",
            "has_mapping": mapping is not None,
        }

    @staticmethod
    def _unique_zimbra(
        values: dict[str, ZimbraAccountIdentity],
    ) -> ZimbraAccountIdentity | None:
        by_id = {
            item.zimbra_id: item
            for item in values.values()
            if item is not None and item.zimbra_id
        }
        if len(by_id) > 1:
            raise ValueError(
                "Найдено несколько разных ящиков Zimbra. Укажите точный e-mail."
            )
        return next(iter(by_id.values()), None)

    def _resolve_by_email(
        self,
        email: str,
    ) -> tuple[ADDirectoryUser | None, ZimbraAccountIdentity | None]:
        ad_service = ActiveDirectoryService(self.settings)
        zimbra_service = ZimbraService(self.settings)

        ad_users = ad_service.users_by_email(email, limit=10)
        if len(ad_users) > 1:
            raise ValueError(
                "AD: по этому e-mail найдено несколько пользователей. "
                "Укажите логин AD."
            )
        ad_user = ad_users[0] if ad_users else None

        zimbra = zimbra_service.account_by_address(email)

        # Если e-mail однозначно разрешился в Zimbra, ее фактический login
        # является хорошей дополнительной подсказкой для общей AD-учетки.
        if ad_user is None and zimbra is not None and zimbra.login:
            ad_user = ad_service.get_user(zimbra.login)

        return ad_user, zimbra

    def _resolve_by_login(
        self,
        record: HRSourceRecord,
        login: str,
    ) -> tuple[ADDirectoryUser, ZimbraAccountIdentity | None, str]:
        ad_service = ActiveDirectoryService(self.settings)
        ad_user = ad_service.get_user(login)
        if ad_user is None:
            raise ValueError(f"AD: логин {login} не найден")

        candidates: list[str] = []

        def add_email(value: str) -> None:
            email = normalize(value)
            if email and "@" in email and email not in candidates:
                candidates.append(email)

        add_email(record.corporate_email)
        add_email(ad_user.email)

        for domain in getattr(self.settings, "zimbra_domains", ()) or ():
            domain = normalize(domain).lstrip("@")
            if domain:
                add_email(f"{ad_user.username}@{domain}")

        zimbra = None
        if candidates:
            zimbra = self._unique_zimbra(
                ZimbraService(self.settings).accounts_by_addresses(candidates)
            )

        best_email = normalize(record.corporate_email)
        if not best_email:
            best_email = normalize(ad_user.email)
        if not best_email and zimbra is not None:
            source_domain = normalize(record.source_id)
            best_email = next(
                (
                    address
                    for address in zimbra.addresses
                    if address.rsplit("@", 1)[-1] == source_domain
                ),
                zimbra.primary_email,
            )

        return ad_user, zimbra, best_email

    def save_identifier(
        self,
        *,
        record_id: int,
        identifier: str,
        actor: str,
    ) -> dict:
        record = self.get_record(record_id)
        value = normalize(identifier)
        if not value:
            raise ValueError("Укажите логин AD или адрес почты")

        is_email = "@" in value
        if is_email:
            ad_user, zimbra = self._resolve_by_email(value)
            source_email = value
        else:
            ad_user, zimbra, source_email = self._resolve_by_login(
                record,
                value,
            )

        if ad_user is None and zimbra is None:
            raise ValueError("Учетная запись не найдена ни в AD, ни в Zimbra")

        source_domain = normalize(record.source_id)
        mapping = self.get_mapping(record)
        created = mapping is None
        if mapping is None:
            mapping = EmailLoginMapping(
                worker_key=record.worker_key,
                source_domain=source_domain,
                source_email="",
                ad_object_guid="",
                ad_login="",
                zimbra_id="",
                zimbra_email="",
                created_by=actor,
            )
            self.db.add(mapping)

        if source_email:
            mapping.source_email = source_email

        if ad_user is not None:
            if not ad_user.object_guid:
                raise ValueError(
                    f"AD: {ad_user.username} найден, но objectGUID не получен"
                )
            mapping.ad_object_guid = ad_user.object_guid
            mapping.ad_login = ad_user.username

        if zimbra is not None:
            if not zimbra.zimbra_id:
                raise ValueError(
                    f"Zimbra: {zimbra.primary_email} найдена, но zimbraId не получен"
                )
            mapping.zimbra_id = zimbra.zimbra_id
            mapping.zimbra_email = zimbra.primary_email

        mapping.updated_at = utcnow()
        mapping.last_verified_at = utcnow()

        self.db.add(
            AuditLog(
                actor=actor,
                action=(
                    "hr_registry_identity_mapping_create"
                    if created
                    else "hr_registry_identity_mapping_update"
                ),
                target=f"{record.source_id}:{record.worker_key}",
                result="success",
                details=json.dumps(
                    {
                        "record_id": record.id,
                        "fio": record.fio,
                        "source_id": record.source_id,
                        "identifier": value,
                        "identifier_type": "email" if is_email else "ad_login",
                        "ad_login": mapping.ad_login,
                        "ad_object_guid": mapping.ad_object_guid,
                        "zimbra_email": mapping.zimbra_email,
                        "zimbra_id": mapping.zimbra_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
        self.db.refresh(mapping)

        return {
            "record_id": record.id,
            "fio": record.fio,
            "source_id": record.source_id,
            "source_email": mapping.source_email,
            "ad_login": mapping.ad_login,
            "zimbra_email": mapping.zimbra_email,
            "has_ad": bool(mapping.ad_object_guid),
            "has_zimbra": bool(mapping.zimbra_id),
        }

    def delete_for_record(
        self,
        *,
        record_id: int,
        actor: str,
    ) -> None:
        record = self.get_record(record_id)
        mapping = self.get_mapping(record)
        if mapping is None:
            return

        self.db.add(
            AuditLog(
                actor=actor,
                action="hr_registry_identity_mapping_delete",
                target=f"{record.source_id}:{record.worker_key}",
                result="success",
                details=json.dumps(
                    {
                        "record_id": record.id,
                        "fio": record.fio,
                        "source_id": record.source_id,
                        "ad_login": mapping.ad_login,
                        "source_email": mapping.source_email,
                        "zimbra_email": mapping.zimbra_email,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.delete(mapping)
        self.db.commit()
