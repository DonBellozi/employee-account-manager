from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OneCAdditionalSource(Base):
    """Дополнительная кадровая выгрузка из общего IMAP-ящика."""

    __tablename__ = "onec_additional_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    mail_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    sender_filter: Mapped[str] = mapped_column(String(512))
    attachment_filename: Mapped[str] = mapped_column(String(512))
    has_corporate_email: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def source_id(self) -> str:
        return self.mail_domain.strip().lower()


class HREmploymentState(Base):
    """Занятость одного человека в одной организации."""

    __tablename__ = "hr_employment_states"
    __table_args__ = (
        UniqueConstraint(
            "worker_key",
            "source_id",
            name="uq_hr_employment_worker_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    fio: Mapped[str] = mapped_column(String(512), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    is_present: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    dismissal_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status_reason: Mapped[str] = mapped_column(String(64), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
