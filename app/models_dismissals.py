from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DismissalDeferral(Base):
    """Отсрочка блокировки для одного окончательного увольнения человека."""

    __tablename__ = "dismissal_deferrals"
    __table_args__ = (
        UniqueConstraint(
            "worker_key",
            "dismissal_date",
            name="uq_dismissal_deferral_worker_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_key: Mapped[str] = mapped_column(String(64), index=True)
    dismissal_date: Mapped[date] = mapped_column(Date, index=True)
    deferred_until: Mapped[date] = mapped_column(Date, index=True)
    operator_username: Mapped[str] = mapped_column(String(256), default="")
    deferral_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
