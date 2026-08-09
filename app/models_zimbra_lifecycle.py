from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ZimbraLifecycleSettings(Base):
    """Разрешения ручного исполнителя жизненного цикла Zimbra."""

    __tablename__ = "zimbra_lifecycle_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    allow_close: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_backup: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_dir: Mapped[str] = mapped_column(String(1024), default="/app/data/zimbra-backups")
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ZimbraLifecycleRun(Base):
    """Один DRY RUN-план или ручной запуск исполнителя."""

    __tablename__ = "zimbra_lifecycle_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), default="plan", index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    observation_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    planned_close: Mapped[int] = mapped_column(Integer, default=0)
    planned_archive: Mapped[int] = mapped_column(Integer, default=0)
    closed_success: Mapped[int] = mapped_column(Integer, default=0)
    backup_success: Mapped[int] = mapped_column(Integer, default=0)
    delete_success: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    operator: Mapped[str] = mapped_column(String(256), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ZimbraLifecycleAction(Base):
    """Детальный шаг плана/исполнения по конкретной учетной записи."""

    __tablename__ = "zimbra_lifecycle_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    account_key: Mapped[str] = mapped_column(String(320), default="", index=True)
    zimbra_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    primary_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    recommendation: Mapped[str] = mapped_column(String(64), default="", index=True)
    action: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    backup_path: Mapped[str] = mapped_column(String(2048), default="")
    backup_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
