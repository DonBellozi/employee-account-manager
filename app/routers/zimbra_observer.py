from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import (
    get_or_create_csrf,
    require_admin,
    validate_csrf,
)
from app.services.zimbra_protection import (
    ManagedZimbraObserverService,
    recommendation_label,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


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
    service = ManagedZimbraObserverService(settings, db)
    states = service.current_states()
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
        ManagedZimbraObserverService(settings, db).save_settings(
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
        run = ManagedZimbraObserverService(settings, db).run(trigger="manual")
        if run.status == "failed":
            message = ""
        elif run.status == "warning":
            message = (
                "Проверка завершена с предупреждением. Рискованные рекомендации "
                "не сформированы без актуальных выгрузок всех известных источников 1С."
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
