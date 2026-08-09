from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models_onec_sources import OneCAdditionalSource
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.zimbra_lifecycle import ZimbraLifecycleService
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))

ACTION_LABELS = {
    "close": "Закрытие",
    "backup_delete": "Backup + удаление",
    "backup": "Backup",
    "delete": "Удаление",
    "skip": "Пропуск",
}
STATUS_LABELS = {
    "planned": "Запланировано",
    "success": "Успешно",
    "blocked": "Не разрешено",
    "failed": "Ошибка",
}


def _zimbra_hr_interlock(db: Session) -> list[OneCAdditionalSource]:
    return list(
        db.scalars(
            select(OneCAdditionalSource)
            .where(
                OneCAdditionalSource.enabled.is_(True),
                OneCAdditionalSource.has_corporate_email.is_(False),
            )
            .order_by(OneCAdditionalSource.name)
        ).all()
    )


def _interlock_message(rows: list[OneCAdditionalSource]) -> str:
    names = ", ".join(row.name for row in rows)
    return (
        "Реальные действия Zimbra отключены: нет надежного e-mail "
        f"сопоставления для источника 1С: {names}."
    )


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    run_id: int | None = None,
    saved: bool = False,
    error: str = "",
):
    current = require_admin(request)
    service = ZimbraLifecycleService(settings, db)
    run = service.get_run(run_id) if run_id else None
    recent = service.recent_runs(limit=20)
    if run is None:
        run = recent[0] if recent else None

    interlock = _zimbra_hr_interlock(db)
    lifecycle = service.settings_view()
    lifecycle["hr_interlock"] = bool(interlock)
    lifecycle["hr_interlock_message"] = (
        _interlock_message(interlock) if interlock else ""
    )

    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "lifecycle": lifecycle,
        "run": run,
        "actions": service.run_actions(run.id) if run is not None else [],
        "runs": recent,
        "action_labels": ACTION_LABELS,
        "status_labels": STATUS_LABELS,
        "saved": saved,
        "error": error,
    }


@router.get("/settings/zimbra-lifecycle")
def lifecycle_page(
    request: Request,
    run_id: int | None = None,
    saved: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "zimbra_lifecycle.html",
        _context(
            request,
            settings=settings,
            db=db,
            run_id=run_id,
            saved=bool(saved),
        ),
    )


@router.post("/settings/zimbra-lifecycle")
def lifecycle_save(
    request: Request,
    csrf: str = Form(...),
    allow_close: str = Form(""),
    allow_backup: str = Form(""),
    allow_delete: str = Form(""),
    backup_dir: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    truthy = {"1", "true", "yes", "on"}
    requested_close = allow_close.strip().lower() in truthy
    requested_backup = allow_backup.strip().lower() in truthy
    requested_delete = allow_delete.strip().lower() in truthy
    try:
        interlock = _zimbra_hr_interlock(db)
        if interlock and (
            requested_close or requested_backup or requested_delete
        ):
            raise ValueError(_interlock_message(interlock))

        ZimbraLifecycleService(settings, db).save_settings(
            allow_close=requested_close,
            allow_backup=requested_backup,
            allow_delete=requested_delete,
            backup_dir=backup_dir,
            operator=current.username,
        )
        return RedirectResponse(
            "/settings/zimbra-lifecycle?saved=1",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_lifecycle.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/zimbra-lifecycle/plan")
def lifecycle_plan(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    run = ZimbraLifecycleService(settings, db).build_plan(current.username)
    return RedirectResponse(
        f"/settings/zimbra-lifecycle?run_id={run.id}",
        status_code=303,
    )


@router.post("/settings/zimbra-lifecycle/execute")
def lifecycle_execute(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        interlock = _zimbra_hr_interlock(db)
        if interlock:
            raise ValueError(_interlock_message(interlock))
        run = ZimbraLifecycleService(settings, db).execute(current.username)
        return RedirectResponse(
            f"/settings/zimbra-lifecycle?run_id={run.id}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_lifecycle.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )
