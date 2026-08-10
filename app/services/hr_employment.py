from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HRSourceRecord
from app.models_onec_sources import HREmploymentState
from app.services.onec_xlsx import OneCWorkbook


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sync_workbook_employment(
    db: Session,
    *,
    workbook: OneCWorkbook,
    source_id: str,
    source_name: str,
    timezone_name: str,
) -> dict[str, int]:
    """Синхронизировать занятость человека в одной организации.

    `worker.dismissal_date` уже учитывает все строки/должности человека:
    дата есть только если каждая должность в этой выгрузке имеет дату
    увольнения. Поэтому одна продолжающаяся должность сохраняет status=active.
    """
    source_id = str(source_id or "").strip().lower()
    source_name = str(source_name or source_id).strip() or source_id
    now = utcnow()
    today = datetime.now(ZoneInfo(timezone_name)).date()
    current_keys = {worker.worker_key for worker in workbook.workers}

    rows = list(
        db.scalars(
            select(HREmploymentState).where(
                HREmploymentState.source_id == source_id
            )
        ).all()
    )
    by_worker = {row.worker_key: row for row in rows}

    source_records = list(
        db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.source_id == source_id
            )
        ).all()
    )
    record_by_worker = {row.worker_key: row for row in source_records}

    active = 0
    scheduled = 0
    dismissed = 0

    for worker in workbook.workers:
        dismissal_date = worker.dismissal_date
        if dismissal_date is None:
            status = "active"
            reason = "current_export"
            active += 1
        elif dismissal_date > today:
            status = "scheduled"
            reason = "dismissal_date"
            scheduled += 1
        else:
            status = "dismissed"
            reason = "dismissal_date"
            dismissed += 1

        employment = by_worker.get(worker.worker_key)
        if employment is None:
            record = record_by_worker.get(worker.worker_key)
            employment = HREmploymentState(
                worker_key=worker.worker_key,
                source_id=source_id,
                source_name=source_name,
                fio=worker.fio,
                first_seen_at=(
                    record.first_seen_at
                    if record is not None
                    else now
                ),
            )
            db.add(employment)
            by_worker[worker.worker_key] = employment

        employment.source_name = source_name
        employment.fio = worker.fio
        employment.status = status
        employment.is_present = True
        employment.dismissal_date = dismissal_date
        employment.status_reason = reason
        employment.last_seen_at = now
        employment.updated_at = now

    absent = 0
    for worker_key, employment in by_worker.items():
        if worker_key in current_keys:
            continue
        record = record_by_worker.get(worker_key)
        if record is not None:
            employment.fio = record.fio
        employment.source_name = source_name
        employment.status = "dismissed"
        employment.is_present = False

        # Бизнес-правило: исчезновение из очередной выгрузки означает
        # увольнение из этой организации. Если реальная прошлая дата уже
        # была известна, сохраняем ее. Если даты не было либо человек исчез
        # раньше ранее запланированной даты, фиксируем первый день, когда
        # отсутствие обнаружено.
        if (
            employment.dismissal_date is None
            or employment.dismissal_date > today
        ):
            employment.dismissal_date = today

        employment.status_reason = "absent_from_export"
        employment.updated_at = now
        absent += 1

    return {
        "active": active,
        "scheduled": scheduled,
        "dismissed_by_date": dismissed,
        "dismissed_by_absence": absent,
    }
