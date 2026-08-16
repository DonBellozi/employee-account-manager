from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SynologyControlSettings(Base):
    """Политика контроля локальных учетных записей Synology DSM."""

    __tablename__ = "synology_control_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    migration_batch_size: Mapped[int] = mapped_column(Integer, default=5)
    migration_interval_days: Mapped[int] = mapped_column(Integer, default=7)
    internal_expiry_months: Mapped[int] = mapped_column(Integer, default=3)
    external_expiry_months: Mapped[int] = mapped_column(Integer, default=6)
    delete_after_months: Mapped[int] = mapped_column(Integer, default=6)

    # В первом этапе интеграция намеренно только наблюдает DSM. Поле заранее
    # резервируется под следующий этап, но Web не позволяет включить его.
    write_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_migration_batch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Предохранитель от массовой блокировки. Если за один прогон к отключению
    # оказалось больше учеток, чем разрешено, этап блокировок не выполняется
    # вообще: одиночная ошибка кадровых данных не должна отключить всех сразу.
    max_disables_per_run: Mapped[int] = mapped_column(Integer, default=10)
    mass_disable_ack_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mass_disable_ack_count: Mapped[int] = mapped_column(Integer, default=0)
    mass_disable_ack_by: Mapped[str] = mapped_column(String(256), default="")

    # Состояние суточного окна блокировок. Счетчик попыток обнуляется вместе
    # со сменой даты: незавершенная работа переносится на следующий вечер.
    block_window_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    block_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_block_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SynologyAccountState(Base):
    """Последнее наблюдаемое и внутреннее lifecycle-состояние DSM-учетки."""

    __tablename__ = "synology_account_states"
    __table_args__ = (
        UniqueConstraint("stable_id", name="uq_synology_account_stable_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stable_id: Mapped[str] = mapped_column(String(128), index=True)
    login: Mapped[str] = mapped_column(String(128), index=True)
    uid: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(320), default="", index=True)
    description: Mapped[str] = mapped_column(String(512), default="")

    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    is_present: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    classification: Mapped[str] = mapped_column(
        String(32), default="unknown", index=True
    )
    worker_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    match_method: Mapped[str] = mapped_column(String(64), default="")

    lifecycle_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cycle_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    policy_expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_applied_expires_at: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )

    dismissal_reference_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, index=True
    )
    delete_after: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    dismissal_reference_source: Mapped[str] = mapped_column(String(32), default="")

    desired_action: Mapped[str] = mapped_column(
        String(32), default="none", index=True
    )
    desired_reason: Mapped[str] = mapped_column(Text, default="")

    last_observed_active: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    last_observed_expires_at: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )

    last_action: Mapped[str] = mapped_column(String(64), default="")
    last_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disabled_reason_code: Mapped[str] = mapped_column(
        String(64), default="", index=True
    )
    attention_state: Mapped[str] = mapped_column(
        String(64), default="", index=True
    )
    attention_details: Mapped[str] = mapped_column(Text, default="")
    attention_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, default="")

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SynologyException(Base):
    """Исключение из любых автоматических действий над локальной DSM-учеткой."""

    __tablename__ = "synology_exceptions"
    __table_args__ = (
        UniqueConstraint("login", name="uq_synology_exception_login"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(128), index=True)
    stable_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    removed_by: Mapped[str] = mapped_column(String(256), default="")
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SynologySyncRun(Base):
    """Короткая история состояния интеграции без шума от каждого цикла."""

    __tablename__ = "synology_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    users_count: Mapped[int] = mapped_column(Integer, default=0)
    new_accounts: Mapped[int] = mapped_column(Integer, default=0)
    changed_accounts: Mapped[int] = mapped_column(Integer, default=0)
    planned_actions: Mapped[int] = mapped_column(Integer, default=0)
    detail_errors: Mapped[int] = mapped_column(Integer, default=0)
    disabled_accounts: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    # Текст сработавшего предохранителя массовой блокировки. Пустая строка
    # означает, что этап блокировок отработал штатно.
    guard_message: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
