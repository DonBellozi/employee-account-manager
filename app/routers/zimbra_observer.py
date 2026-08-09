from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import (
    get_current_user,
    get_or_create_csrf,
    require_admin,
    validate_csrf,
)
from app.services.zimbra_observer import (
    ZimbraObserverService,
    recommendation_label,
)
from app.time_utils import format_app_datetime, register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _event_label(recommendation: str, previous: str) -> str:
    if recommendation == "none" and previous:
        return "Рекомендация снята"
    return recommendation_label(recommendation)


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    saved: bool = False,
    run_message: str = "",
    error: str = "",
):
    current = require_admin(request)
    service = ZimbraObserverService(settings, db)
    states = service.current_states(limit=300)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "observer": service.settings_view(),
        "latest_run": service.latest_run(),
        "runs": service.recent_runs(limit=20),
        "states": states,
        "state_labels": {
            state.recommendation: recommendation_label(state.recommendation)
            for state in states
        },
        "saved": saved,
        "run_message": run_message,
        "error": error,
    }


@router.get("/settings/zimbra-observer")
def observer_page(
    request: Request,
    saved: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "zimbra_observer.html",
        _context(
            request,
            settings=settings,
            db=db,
            saved=bool(saved),
        ),
    )


@router.post("/settings/zimbra-observer")
def observer_save(
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(""),
    inactive_months: int = Form(...),
    retention_months: int = Form(...),
    schedule_time: str = Form(...),
    exclude_active_hr: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        ZimbraObserverService(settings, db).save_settings(
            enabled=enabled.strip().lower() in {"1", "true", "yes", "on"},
            inactive_months=inactive_months,
            retention_months=retention_months,
            schedule_time=schedule_time,
            exclude_active_hr=(
                exclude_active_hr.strip().lower() in {"1", "true", "yes", "on"}
            ),
            operator=current.username,
        )
        return RedirectResponse("/settings/zimbra-observer?saved=1", status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_observer.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/zimbra-observer/run")
def observer_run_now(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        run = ZimbraObserverService(settings, db).run(trigger="manual")
        if run.status == "failed":
            message = ""
        elif run.status == "warning":
            message = (
                "Проверка завершена с предупреждением. Рискованные рекомендации "
                "не сформированы без свежего реестра 1С."
            )
        else:
            message = (
                f"Проверка завершена: закрыть – {run.close_candidates}, "
                f"архивировать/удалить – {run.archive_candidates}, "
                f"новых событий журнала – {run.event_count}."
            )
        return templates.TemplateResponse(
            request,
            "zimbra_observer.html",
            _context(
                request,
                settings=settings,
                db=db,
                run_message=message,
                error=(run.error_message if run.status == "failed" else ""),
            ),
            status_code=200 if run.status != "failed" else 503,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_observer.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=503,
        )


@router.get("/zimbra-observer/journal")
def observer_journal_api(
    request: Request,
    limit: int = 30,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    get_current_user(request)
    service = ZimbraObserverService(settings, db)
    latest = service.latest_run()
    events = service.recent_events(limit=max(1, min(limit, 100)))
    return {
        "ok": True,
        "mode": "observe_only",
        "latest_run": (
            {
                "id": latest.id,
                "status": latest.status,
                "started_at": format_app_datetime(latest.started_at),
                "completed_at": format_app_datetime(latest.completed_at),
                "close_candidates": latest.close_candidates,
                "archive_candidates": latest.archive_candidates,
                "protected_by_hr": latest.protected_by_hr,
                "manual_review": latest.manual_review,
                "event_count": latest.event_count,
                "error": latest.error_message,
            }
            if latest is not None
            else None
        ),
        "events": [
            {
                "id": item.id,
                "created_at": format_app_datetime(item.created_at),
                "email": item.primary_email,
                "recommendation": item.recommendation,
                "recommendation_label": _event_label(
                    item.recommendation, item.previous_recommendation
                ),
                "previous_recommendation": item.previous_recommendation,
                "reason": item.reason,
                "account_status": item.account_status,
                "last_logon_at": format_app_datetime(
                    item.last_logon_at, "%d.%m.%Y %H:%M"
                ),
                "hr_active": bool(item.hr_active),
            }
            for item in events
        ],
    }
