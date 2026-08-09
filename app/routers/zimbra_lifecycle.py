from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
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
    if run is None:
        recent = service.recent_runs(limit=20)
        run = recent[0] if recent else None
    else:
        recent = service.recent_runs(limit=20)

    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "lifecycle": service.settings_view(),
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
    try:
        ZimbraLifecycleService(settings, db).save_settings(
            allow_close=allow_close.strip().lower() in truthy,
            allow_backup=allow_backup.strip().lower() in truthy,
            allow_delete=allow_delete.strip().lower() in truthy,
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
    run = ZimbraLifecycleService(settings, db).execute(current.username)
    return RedirectResponse(
        f"/settings/zimbra-lifecycle?run_id={run.id}",
        status_code=303,
    )
