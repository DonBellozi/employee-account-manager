from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TechExpertSettings(Base):
    """Настройки уведомительного контура, управляемые из Web."""

    __tablename__ = "techexpert_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    source_domain: Mapped[str] = mapped_column(String(255), default="")
    ad_group_dn: Mapped[str] = mapped_column(String(1024), default="")
    recipient_email: Mapped[str] = mapped_column(String(320), default="")
    notification_time: Mapped[str] = mapped_column(String(5), default="08:45")
    subject: Mapped[str] = mapped_column(String(512))
    body_html: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class TechExpertNotification(Base):
    """Одно письмо в Техэксперт на один кадровый эпизод организации."""

    __tablename__ = "techexpert_notifications"
    __table_args__ = (
        UniqueConstraint(
            "employment_event_id",
            name="uq_techexpert_notification_employment_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employment_event_id: Mapped[int] = mapped_column(Integer, index=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    fio: Mapped[str] = mapped_column(String(512), default="")
    corporate_email: Mapped[str] = mapped_column(String(320), default="")
    ad_login: Mapped[str] = mapped_column(String(128), default="")
    ad_object_guid: Mapped[str] = mapped_column(String(64), default="")
    dismissal_date: Mapped[date] = mapped_column(Date, index=True)
    deferred_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    hr_reason: Mapped[str] = mapped_column(String(64), default="")
    event_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recipient_email: Mapped[str] = mapped_column(String(320), default="")
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
    )
    membership_state: Mapped[str] = mapped_column(
        String(32),
        default="not_checked",
    )
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
    attention_state: Mapped[str] = mapped_column(
        String(64),
        default="",
        index=True,
    )
    attention_details: Mapped[str] = mapped_column(Text, default="")
    attention_at: Mapped[datetime | None] = mapped_column(
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
