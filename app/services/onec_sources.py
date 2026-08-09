from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    EmailLoginMapping,
    HRSourceRecord,
    OneCImportRun,
)
from app.models_onec_sources import (
    HREmploymentState,
    OneCAdditionalSource,
)


DOMAIN_RE = re.compile(
    r"^(?=.{1,255}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OneCSourceRegistryService:
    """Web-настройки кадровых источников 1С."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @staticmethod
    def normalize_domain(value: str) -> str:
        domain = str(value or "").strip().lower().lstrip("@")
        if not domain or not DOMAIN_RE.fullmatch(domain):
            raise ValueError("Укажите корректный домен выгрузки 1С")
        return domain

    @staticmethod
    def _clean_required(value: str, label: str) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            raise ValueError(f"Укажите {label}")
        return text

    @staticmethod
    def _clean_folder(value: str) -> str:
        folder = str(value or "").strip()
        if not folder:
            raise ValueError("Укажите папку IMAP")
        if any(ord(char) < 32 for char in folder):
            raise ValueError("Папка IMAP содержит недопустимые символы")
        return folder

    @staticmethod
    def _clean_sender(value: str) -> str:
        value = str(value or "").strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError("Фильтр отправителя содержит недопустимые символы")
        return value

    def _existing_source_domains(self) -> set[str]:
        return {
            str(value or "").strip().lower()
            for value in self.db.scalars(
                select(OneCAdditionalSource.mail_domain)
            ).all()
            if str(value or "").strip()
        }

    def _infer_primary_domain(self) -> str:
        explicit = self.settings.onec_source_domain.strip().lower().lstrip("@")
        if explicit:
            return explicit

        configured = self._existing_source_domains()

        record_domains = {
            str(value or "").strip().lower()
            for value in self.db.scalars(
                select(HRSourceRecord.source_id)
            ).all()
            if str(value or "").strip().lower() not in {"", "org_com"}
        }
        candidates = sorted(record_domains - configured)
        if len(candidates) == 1:
            return candidates[0]

        run_domains = {
            str(value or "").strip().lower()
            for value in self.db.scalars(
                select(OneCImportRun.source_id).where(
                    OneCImportRun.status.in_(("success", "partial", "duplicate"))
                )
            ).all()
            if str(value or "").strip().lower() not in {"", "org_com"}
        }
        candidates = sorted(run_domains - configured)
        if len(candidates) == 1:
            return candidates[0]

        return ""

    def ensure_primary(self) -> OneCAdditionalSource:
        primary = self.db.scalar(
            select(OneCAdditionalSource)
            .where(OneCAdditionalSource.is_primary.is_(True))
            .order_by(OneCAdditionalSource.id)
            .limit(1)
        )
        if primary is not None:
            return primary

        domain = self._infer_primary_domain()
        existing = None
        if domain:
            existing = self.db.scalar(
                select(OneCAdditionalSource).where(
                    OneCAdditionalSource.mail_domain == domain
                )
            )

        if existing is not None:
            existing.is_primary = True
            existing.imap_folder = (
                existing.imap_folder.strip()
                or self.settings.onec_imap_folder.strip()
                or "INBOX"
            )
            primary = existing
        else:
            primary = OneCAdditionalSource(
                name="Основная компания",
                mail_domain=domain,
                imap_folder=self.settings.onec_imap_folder.strip() or "INBOX",
                sender_filter=self.settings.onec_imap_from_contains.strip(),
                attachment_filename=self.settings.onec_attachment_filename.strip(),
                is_primary=True,
                enabled=True,
                updated_by="migration",
            )
            self.db.add(primary)

        self.db.commit()
        self.db.refresh(primary)
        return primary

    def primary_source(self) -> OneCAdditionalSource:
        return self.ensure_primary()

    def apply_primary_to_settings(
        self,
        source: OneCAdditionalSource | None = None,
    ) -> OneCAdditionalSource:
        primary = source or self.ensure_primary()
        if not primary.is_primary:
            raise ValueError("Указанный источник не является основным")
        self.settings.onec_imap_folder = primary.imap_folder.strip() or "INBOX"
        self.settings.onec_imap_from_contains = primary.sender_filter.strip()
        self.settings.onec_attachment_filename = primary.attachment_filename.strip()
        self.settings.onec_source_domain = primary.mail_domain.strip().lower()
        return primary

    def list_sources(self) -> list[OneCAdditionalSource]:
        self.ensure_primary()
        return list(
            self.db.scalars(
                select(OneCAdditionalSource).order_by(
                    OneCAdditionalSource.is_primary.desc(),
                    OneCAdditionalSource.enabled.desc(),
                    OneCAdditionalSource.name,
                    OneCAdditionalSource.mail_domain,
                )
            ).all()
        )

    def enabled_sources(
        self,
        *,
        include_primary: bool = True,
    ) -> list[OneCAdditionalSource]:
        self.ensure_primary()
        query = (
            select(OneCAdditionalSource)
            .where(OneCAdditionalSource.enabled.is_(True))
            .order_by(
                OneCAdditionalSource.is_primary.desc(),
                OneCAdditionalSource.name,
                OneCAdditionalSource.id,
            )
        )
        if not include_primary:
            query = query.where(OneCAdditionalSource.is_primary.is_(False))
        return list(self.db.scalars(query).all())

    def get(self, source_id: int) -> OneCAdditionalSource:
        row = self.db.get(OneCAdditionalSource, int(source_id))
        if row is None:
            raise LookupError("Кадровый источник не найден")
        return row

    def _migrate_source_domain(
        self,
        *,
        old_domain: str,
        new_domain: str,
        source_name: str,
    ) -> None:
        old_domain = str(old_domain or "").strip().lower()
        new_domain = str(new_domain or "").strip().lower()
        if not old_domain or old_domain == new_domain:
            if new_domain:
                for record in self.db.scalars(
                    select(HRSourceRecord).where(
                        HRSourceRecord.source_id == new_domain
                    )
                ).all():
                    record.source_name = source_name
                for state in self.db.scalars(
                    select(HREmploymentState).where(
                        HREmploymentState.source_id == new_domain
                    )
                ).all():
                    state.source_name = source_name
            return

        if self.db.scalar(
            select(HRSourceRecord.id)
            .where(HRSourceRecord.source_id == new_domain)
            .limit(1)
        ) is not None:
            raise ValueError(
                "Новый домен уже используется кадровыми данными другого источника"
            )
        if self.db.scalar(
            select(EmailLoginMapping.id)
            .where(EmailLoginMapping.source_domain == new_domain)
            .limit(1)
        ) is not None:
            raise ValueError(
                "Новый домен уже используется сопоставлениями другого источника"
            )

        for record in self.db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == old_domain
            )
        ).all():
            record.source_id = new_domain
            record.source_name = source_name

        for state in self.db.scalars(
            select(HREmploymentState).where(
                HREmploymentState.source_id == old_domain
            )
        ).all():
            state.source_id = new_domain
            state.source_name = source_name

        for run in self.db.scalars(
            select(OneCImportRun).where(
                OneCImportRun.source_id == old_domain
            )
        ).all():
            run.source_id = new_domain

        for mapping in self.db.scalars(
            select(EmailLoginMapping).where(
                EmailLoginMapping.source_domain == old_domain
            )
        ).all():
            mapping.source_domain = new_domain

    def save(
        self,
        *,
        source_id: int | None,
        name: str,
        mail_domain: str,
        imap_folder: str,
        sender_filter: str,
        attachment_filename: str,
        enabled: bool,
        operator: str,
    ) -> OneCAdditionalSource:
        normalized_domain = self.normalize_domain(mail_domain)
        clean_name = self._clean_required(name, "название организации")
        clean_folder = self._clean_folder(imap_folder)
        clean_sender = self._clean_sender(sender_filter)
        clean_filename = self._clean_required(
            attachment_filename,
            "имя вложения",
        )

        duplicate_domain = self.db.scalar(
            select(OneCAdditionalSource).where(
                OneCAdditionalSource.mail_domain == normalized_domain
            )
        )
        if duplicate_domain is not None and (
            source_id is None or duplicate_domain.id != int(source_id)
        ):
            raise ValueError("Источник с таким доменом уже существует")

        duplicate_pair = self.db.scalar(
            select(OneCAdditionalSource).where(
                OneCAdditionalSource.imap_folder == clean_folder,
                OneCAdditionalSource.sender_filter == clean_sender,
                OneCAdditionalSource.attachment_filename == clean_filename,
            )
        )
        if duplicate_pair is not None and (
            source_id is None or duplicate_pair.id != int(source_id)
        ):
            raise ValueError(
                "Источник с такой папкой, отправителем и именем вложения уже существует"
            )

        row = self.get(source_id) if source_id else OneCAdditionalSource()
        old_domain = row.mail_domain if source_id else ""
        if source_id is None:
            row.is_primary = False

        self._migrate_source_domain(
            old_domain=old_domain,
            new_domain=normalized_domain,
            source_name=clean_name,
        )

        row.name = clean_name
        row.mail_domain = normalized_domain
        row.imap_folder = clean_folder
        row.sender_filter = clean_sender
        row.attachment_filename = clean_filename
        row.enabled = bool(enabled)
        row.updated_by = str(operator or "")[:256]
        row.updated_at = utcnow()
        if source_id is None:
            self.db.add(row)

        self.db.commit()
        self.db.refresh(row)

        if row.is_primary:
            self.apply_primary_to_settings(row)

        return row

    def set_enabled(
        self,
        source_id: int,
        *,
        enabled: bool,
        operator: str,
    ) -> OneCAdditionalSource:
        row = self.get(source_id)
        row.enabled = bool(enabled)
        row.updated_by = str(operator or "")[:256]
        row.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    def latest_run(self, source: OneCAdditionalSource) -> OneCImportRun | None:
        if not source.source_id:
            return None
        return self.db.scalars(
            select(OneCImportRun)
            .where(OneCImportRun.source_id == source.source_id)
            .order_by(desc(OneCImportRun.started_at), desc(OneCImportRun.id))
            .limit(1)
        ).first()

    def page_rows(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for source in self.list_sources():
            run = self.latest_run(source)
            result.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "mail_domain": source.mail_domain,
                    "imap_folder": source.imap_folder,
                    "sender_filter": source.sender_filter,
                    "attachment_filename": source.attachment_filename,
                    "is_primary": source.is_primary,
                    "enabled": source.enabled,
                    "updated_by": source.updated_by,
                    "updated_at": source.updated_at,
                    "last_status": run.status if run else "",
                    "last_started_at": run.started_at if run else None,
                    "last_completed_at": run.completed_at if run else None,
                    "last_workers_count": run.workers_count if run else 0,
                    "last_error": run.error_message if run else "",
                }
            )
        return result
