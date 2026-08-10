from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models_onec_polling import OneCSourcePollState
from app.models_onec_sources import OneCAdditionalSource


CONTROL_EXPORT_TIME = time(19, 0)
CONTROL_EXPORT_TIME_LABEL = "19:00"
ACCEPTED_POLL_STATUSES = {"success", "partial", "duplicate"}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class OneCSourceFreshness:
    source_id: str
    source_name: str
    ready: bool
    status: str
    message_at: datetime | None
    reason: str


class OneCSourceFreshnessService:
    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self.tz = ZoneInfo(settings.app_timezone)

    def _today(self) -> date:
        return datetime.now(self.tz).date()

    def source_state(
        self,
        source: OneCAdditionalSource,
        *,
        expected_date: date | None = None,
    ) -> OneCSourceFreshness:
        day = expected_date or self._today()
        row = self.db.scalar(
            select(OneCSourcePollState).where(
                OneCSourcePollState.source_id == source.source_id
            )
        )
        if row is None:
            return OneCSourceFreshness(
                source_id=source.source_id,
                source_name=source.name,
                ready=False,
                status="never",
                message_at=None,
                reason="Источник еще не проверялся IMAP-worker'ом",
            )
        if row.last_status not in ACCEPTED_POLL_STATUSES:
            return OneCSourceFreshness(
                source_id=source.source_id,
                source_name=source.name,
                ready=False,
                status=row.last_status,
                message_at=row.last_message_at,
                reason=row.last_error or "Последняя выгрузка не принята",
            )
        if row.last_message_at is None:
            return OneCSourceFreshness(
                source_id=source.source_id,
                source_name=source.name,
                ready=False,
                status=row.last_status,
                message_at=None,
                reason="Не удалось определить дату письма с выгрузкой",
            )

        local_message = _aware(row.last_message_at).astimezone(self.tz)
        if local_message.date() != day:
            return OneCSourceFreshness(
                source_id=source.source_id,
                source_name=source.name,
                ready=False,
                status=row.last_status,
                message_at=row.last_message_at,
                reason="Нет контрольной выгрузки за текущий день",
            )
        if local_message.time().replace(tzinfo=None) < CONTROL_EXPORT_TIME:
            return OneCSourceFreshness(
                source_id=source.source_id,
                source_name=source.name,
                ready=False,
                status=row.last_status,
                message_at=row.last_message_at,
                reason=(
                    "Последняя принятая выгрузка отправлена до "
                    + CONTROL_EXPORT_TIME_LABEL
                ),
            )

        return OneCSourceFreshness(
            source_id=source.source_id,
            source_name=source.name,
            ready=True,
            status=row.last_status,
            message_at=row.last_message_at,
            reason="Контрольная выгрузка получена",
        )

    def all_control_exports_ready(
        self,
        *,
        expected_date: date | None = None,
    ) -> bool:
        sources = list(
            self.db.scalars(
                select(OneCAdditionalSource)
                .where(OneCAdditionalSource.enabled.is_(True))
                .order_by(OneCAdditionalSource.id)
            ).all()
        )
        if not sources:
            return False
        return all(
            self.source_state(
                source,
                expected_date=expected_date,
            ).ready
            for source in sources
        )
