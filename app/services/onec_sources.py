from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import OneCImportRun
from app.models_onec_sources import OneCAdditionalSource


DOMAIN_RE = re.compile(
    r"^(?=.{1,255}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OneCSourceRegistryService:
    """Настройки дополнительных кадровых источников 1С."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @staticmethod
    def normalize_domain(value: str) -> str:
        domain = str(value or "").strip().lower().lstrip("@")
        if not domain or not DOMAIN_RE.fullmatch(domain):
            raise ValueError("Укажите корректный почтовый домен организации")
        return domain

    @staticmethod
    def _clean_required(value: str, label: str) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            raise ValueError(f"Укажите {label}")
        return text

    def primary_source(self) -> dict[str, object]:
        return {
            "name": "Основной источник",
            "mail_domain": self.settings.onec_source_domain.strip().lower(),
            "sender_filter": self.settings.onec_imap_from_contains.strip(),
            "attachment_filename": self.settings.onec_attachment_filename.strip(),
            "enabled": True,
            "managed_in_env": True,
        }

    def list_sources(self) -> list[OneCAdditionalSource]:
        return list(
            self.db.scalars(
                select(OneCAdditionalSource).order_by(
                    OneCAdditionalSource.enabled.desc(),
                    OneCAdditionalSource.name,
                    OneCAdditionalSource.mail_domain,
                )
            ).all()
        )

    def enabled_sources(self) -> list[OneCAdditionalSource]:
        return list(
            self.db.scalars(
                select(OneCAdditionalSource)
                .where(OneCAdditionalSource.enabled.is_(True))
                .order_by(OneCAdditionalSource.name, OneCAdditionalSource.id)
            ).all()
        )

    def get(self, source_id: int) -> OneCAdditionalSource:
        row = self.db.get(OneCAdditionalSource, int(source_id))
        if row is None:
            raise LookupError("Кадровый источник не найден")
        return row

    def save(
        self,
        *,
        source_id: int | None,
        name: str,
        mail_domain: str,
        sender_filter: str,
        attachment_filename: str,
        has_corporate_email: bool,
        enabled: bool,
        operator: str,
    ) -> OneCAdditionalSource:
        normalized_domain = self.normalize_domain(mail_domain)
        clean_name = self._clean_required(name, "название организации")
        clean_sender = self._clean_required(sender_filter, "отправителя")
        clean_filename = self._clean_required(
            attachment_filename,
            "имя файла вложения",
        )

        primary_domain = self.settings.onec_source_domain.strip().lower().lstrip("@")
        if primary_domain and normalized_domain == primary_domain:
            raise ValueError(
                "Этот домен уже используется основным источником 1С"
            )

        duplicate_domain = self.db.scalar(
            select(OneCAdditionalSource).where(
                OneCAdditionalSource.mail_domain == normalized_domain
            )
        )
        if duplicate_domain is not None and (
            source_id is None or duplicate_domain.id != int(source_id)
        ):
            raise ValueError("Источник с таким почтовым доменом уже существует")

        duplicate_pair = self.db.scalar(
            select(OneCAdditionalSource).where(
                OneCAdditionalSource.sender_filter == clean_sender,
                OneCAdditionalSource.attachment_filename == clean_filename,
            )
        )
        if duplicate_pair is not None and (
            source_id is None or duplicate_pair.id != int(source_id)
        ):
            raise ValueError(
                "Источник с таким отправителем и именем файла уже существует"
            )

        row = self.get(source_id) if source_id else OneCAdditionalSource()
        row.name = clean_name
        row.mail_domain = normalized_domain
        row.sender_filter = clean_sender
        row.attachment_filename = clean_filename
        row.has_corporate_email = bool(has_corporate_email)
        row.enabled = bool(enabled)
        row.updated_by = str(operator or "")[:256]
        row.updated_at = utcnow()
        if source_id is None:
            self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
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
                    "sender_filter": source.sender_filter,
                    "attachment_filename": source.attachment_filename,
                    "has_corporate_email": source.has_corporate_email,
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
