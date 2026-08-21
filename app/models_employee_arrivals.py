from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HREmploymentArrivalSourceState(Base):
    """Точка отсчета уведомлений о новых появлениях для кадрового источника."""

    __tablename__ = "hr_employment_arrival_source_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    initialized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class HREmploymentArrivalEvent(Base):
    """Один эпизод появления человека в одной организации."""

    __tablename__ = "hr_employment_arrival_events"
    __table_args__ = (
        UniqueConstraint(
            "worker_key",
            "source_id",
            "sequence",
            name="uq_hr_employment_arrival_episode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    fio: Mapped[str] = mapped_column(String(512), index=True)
    arrival_kind: Mapped[str] = mapped_column(
        String(32),
        default="employee",
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
    )
    decision_by: Mapped[str] = mapped_column(String(256), default="")
    decision_details: Mapped[str] = mapped_column(Text, default="")
    provisioning_operation_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
