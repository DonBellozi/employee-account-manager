from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DismissalEquipmentNotice(Base):
    """Одна рассылка о возврате оборудования на один эпизод увольнения."""

    __tablename__ = "dismissal_equipment_notices"
    __table_args__ = (
        UniqueConstraint(
            "worker_key",
            "dismissal_date",
            name="uq_dismissal_notice_worker_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    dismissal_date: Mapped[date] = mapped_column(Date, index=True)
    fio: Mapped[str] = mapped_column(String(512), default="")
    sender_domain: Mapped[str] = mapped_column(String(255), default="")
    event_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    recipients_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class HREmploymentDismissalEvent(Base):
    """Устойчивый эпизод появления даты увольнения в одной организации.

    Изменение или временное исчезновение даты обновляет этот же эпизод и не
    создает повторное письмо. Новый эпизод начинается только после повторного
    появления работника после фактического отсутствия из кадровой выгрузки.
    """

    __tablename__ = "hr_employment_dismissal_events"
    __table_args__ = (
        UniqueConstraint(
            "worker_key", "source_id", "sequence",
            name="uq_hr_dismissal_event_worker_source_sequence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    fio: Mapped[str] = mapped_column(String(512), default="")
    first_dismissal_date: Mapped[date] = mapped_column(Date)
    current_dismissal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    noticed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
