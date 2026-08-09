from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ZimbraProtectedAccount(Base):
    """Управляемое Web-исключение из жизненного цикла Zimbra."""

    __tablename__ = "zimbra_protected_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zimbra_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    primary_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    display_name: Mapped[str] = mapped_column(String(512), default="", index=True)
    source: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    activated_by: Mapped[str] = mapped_column(String(256), default="")
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deactivated_by: Mapped[str] = mapped_column(String(256), default="")
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ZimbraProtectionMigration(Base):
    """Фиксирует переход источника защиты с zimbraNotes на Web-БД."""

    __tablename__ = "zimbra_protection_migration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_by: Mapped[str] = mapped_column(String(256), default="")
    last_import_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_import_by: Mapped[str] = mapped_column(String(256), default="")


class ZimbraProtectionEvent(Base):
    """Неизменяемая история включения и снятия Web-защиты."""

    __tablename__ = "zimbra_protection_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protection_id: Mapped[int] = mapped_column(Integer, index=True)
    zimbra_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    primary_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    display_name: Mapped[str] = mapped_column(String(512), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(256), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
