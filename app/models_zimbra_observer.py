from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ZimbraObserverSettings(Base):
    """Web-настройки безопасного наблюдения за жизненным циклом Zimbra."""

    __tablename__ = "zimbra_observer_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    inactive_months: Mapped[int] = mapped_column(Integer, default=6)
    retention_months: Mapped[int] = mapped_column(Integer, default=12)
    schedule_time: Mapped[str] = mapped_column(String(5), default="08:30")
    exclude_active_hr: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ZimbraObservationRun(Base):
    """Один запуск read-only проверки Zimbra."""

    __tablename__ = "zimbra_observation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    total_accounts: Mapped[int] = mapped_column(Integer, default=0)
    relevant_accounts: Mapped[int] = mapped_column(Integer, default=0)
    close_candidates: Mapped[int] = mapped_column(Integer, default=0)
    archive_candidates: Mapped[int] = mapped_column(Integer, default=0)
    protected_by_hr: Mapped[int] = mapped_column(Integer, default=0)
    manual_review: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    hr_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hr_snapshot_age_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ZimbraLifecycleState(Base):
    """Последний известный вывод наблюдателя по конкретной учетной записи."""

    __tablename__ = "zimbra_lifecycle_states"
    __table_args__ = (
        UniqueConstraint("account_key", name="uq_zimbra_lifecycle_account_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), index=True)
    zimbra_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    primary_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    addresses_json: Mapped[str] = mapped_column(Text, default="[]")
    account_status: Mapped[str] = mapped_column(String(64), default="")
    last_logon_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_in_zimbra_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    zimbra_note: Mapped[str] = mapped_column(Text, default="")
    hr_active: Mapped[bool] = mapped_column(Boolean, default=False)
    matched_hr_email: Mapped[str] = mapped_column(String(320), default="")
    recommendation: Mapped[str] = mapped_column(String(64), default="none", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    first_observed_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    last_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class ZimbraObservationEvent(Base):
    """Событие журнала: вывод наблюдателя изменился."""

    __tablename__ = "zimbra_observation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    account_key: Mapped[str] = mapped_column(String(320), index=True)
    zimbra_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    primary_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    previous_recommendation: Mapped[str] = mapped_column(String(64), default="")
    recommendation: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    account_status: Mapped[str] = mapped_column(String(64), default="")
    last_logon_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hr_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
