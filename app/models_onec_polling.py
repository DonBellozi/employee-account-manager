from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OneCSourcePollState(Base):
    """Техническое состояние периодического IMAP-опроса одного источника 1С."""

    __tablename__ = "onec_source_poll_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_name: Mapped[str] = mapped_column(String(256), default="")
    config_key: Mapped[str] = mapped_column(String(64), default="", index=True)

    # Последний UID, до которого почтовый ящик полностью просмотрен.
    last_scanned_uid: Mapped[str] = mapped_column(String(128), default="")

    # Последняя подтвержденная кадровая выгрузка.
    last_attachment_uid: Mapped[str] = mapped_column(String(128), default="")
    last_file_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    last_message_date: Mapped[str] = mapped_column(String(256), default="")
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    last_status: Mapped[str] = mapped_column(String(32), default="never", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # При ошибке тот же UID повторно пробуем не чаще указанного времени.
    failed_uid: Mapped[str] = mapped_column(String(128), default="")
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
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
