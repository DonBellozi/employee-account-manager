from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord
from app.models_dismissal_lifecycle import (
    ADReactivationAlert,
    FinalDismissalBlockRun,
    FinalDismissalBlockTarget,
)
from app.models_notifications import HREmploymentDismissalEvent
from app.models_onec_sources import HREmploymentState
from app.services.dismissal_notifications import DismissalNotificationService
from app.services.final_dismissal_lifecycle import FinalDismissalLifecycleService


class FakeSettings:
    app_timezone = "UTC"
    dry_run = False


def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_employment(db: Session, source_id: str, dismissal_date: date | None):
    db.add(
        HREmploymentState(
            worker_key="worker-1",
            source_id=source_id,
            source_name=source_id,
            fio="Иванов Иван",
            status="scheduled" if dismissal_date else "active",
            is_present=True,
            dismissal_date=dismissal_date,
        )
    )
    db.commit()


def test_date_move_clear_and_restore_stay_in_one_event():
    db = session()
    add_employment(db, "org-a.ru", date(2030, 1, 10))
    service = DismissalNotificationService(FakeSettings(), db)
    service._sync_employment_events()

    state = db.scalar(select(HREmploymentState))
    state.dismissal_date = date(2030, 1, 20)
    service._sync_employment_events()
    state.dismissal_date = None
    state.status = "active"
    service._sync_employment_events()
    state.dismissal_date = date(2030, 1, 25)
    state.status = "scheduled"
    service._sync_employment_events()

    events = list(db.scalars(select(HREmploymentDismissalEvent)).all())
    assert len(events) == 1
    assert events[0].first_dismissal_date == date(2030, 1, 10)
    assert events[0].current_dismissal_date == date(2030, 1, 25)


def test_simultaneous_organizations_are_one_worker_event_candidate():
    db = session()
    add_employment(db, "org-a.ru", date(2030, 1, 10))
    add_employment(db, "org-b.ru", date(2030, 1, 10))
    db.add_all(
        [
            HRSourceRecord(
                worker_key="worker-1",
                source_id="org-a.ru",
                fio="Иванов Иван",
                corporate_email="ivanov@org-a.ru",
            ),
            HRSourceRecord(
                worker_key="worker-1",
                source_id="org-b.ru",
                fio="Иванов Иван",
                corporate_email="ivanov@org-b.ru",
            ),
        ]
    )
    db.commit()
    service = DismissalNotificationService(FakeSettings(), db)
    service._sync_employment_events()
    events = list(db.scalars(select(HREmploymentDismissalEvent)).all())

    candidate = service._event_candidate(events)
    assert {item["source_id"] for item in candidate["organizations"]} == {
        "org-a.ru",
        "org-b.ru",
    }
    _, recipients = service._event_recipient_plan(
        candidate,
        {"org-a.ru": object(), "org-b.ru": object()},
    )
    assert {item["email"] for item in recipients} == {
        "ivanov@org-a.ru",
        "ivanov@org-b.ru",
    }


def test_shared_lifecycle_creates_only_ad_target():
    db = session()
    add_employment(db, "org-a.ru", date(2030, 1, 10))
    db.add(HRSourceRecord(worker_key="worker-1", source_id="org-a.ru", fio="Иванов Иван", login="ivanov"))
    db.add(
        EmailLoginMapping(
            worker_key="worker-1",
            source_domain="org-a.ru",
            source_email="ivanov@org-a.ru",
            ad_object_guid="",
            ad_login="ivanov",
            zimbra_id="",
            zimbra_email="",
        )
    )
    db.commit()

    service = FinalDismissalLifecycleService(FakeSettings(), db)
    run = service._ensure_run(
        {
            "worker_key": "worker-1",
            "dismissal_date": date(2030, 1, 10),
            "effective_block_date": date(2030, 1, 17),
            "fio": "Иванов Иван",
        }
    )
    targets = list(
        db.scalars(
            select(FinalDismissalBlockTarget).where(
                FinalDismissalBlockTarget.run_id == run.id
            )
        ).all()
    )
    assert [target.system for target in targets] == ["ad"]


def test_active_again_creates_alert_without_enabling_ad():
    db = session()
    add_employment(db, "org-a.ru", None)
    run = FinalDismissalBlockRun(
        worker_key="worker-1",
        dismissal_date=date(2029, 1, 1),
        effective_block_date=date(2029, 1, 8),
        fio="Иванов Иван",
        status="success",
    )
    db.add(run)
    db.flush()
    db.add(
        FinalDismissalBlockTarget(
            run_id=run.id,
            system="ad",
            target_key="ad:ivanov",
            target_identifier="ivanov",
            status="completed",
        )
    )
    db.commit()

    FinalDismissalLifecycleService(FakeSettings(), db)._create_reactivation_alerts()
    alert = db.scalar(select(ADReactivationAlert))
    assert alert is not None
    assert alert.status == "open"
    assert "Автоматическое включение запрещено" in alert.details
