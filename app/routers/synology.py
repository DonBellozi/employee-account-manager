from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.synology import SynologyService
from app.services.synology_lifecycle import SynologyLifecycleService
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    saved: bool = False,
    exception_saved: bool = False,
    error: str = "",
):
    current = require_admin(request)
    service = SynologyLifecycleService(settings, db)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "synology": service.view(),
        "saved": saved,
        "exception_saved": exception_saved,
        "error": error,
    }


@router.get("/settings/synology")
def synology_page(
    request: Request,
    saved: int = 0,
    exception_saved: int = 0,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "synology.html",
        _context(
            request,
            settings=settings,
            db=db,
            saved=bool(saved),
            exception_saved=bool(exception_saved),
        ),
    )


@router.post("/settings/synology/policy")
def synology_policy_save(
    request: Request,
    csrf: str = Form(...),
    sync_interval_minutes: int = Form(...),
    migration_batch_size: int = Form(...),
    migration_interval_days: int = Form(...),
    internal_expiry_months: int = Form(...),
    external_expiry_months: int = Form(...),
    delete_after_months: int = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        SynologyLifecycleService(settings, db).save_control_settings(
            sync_interval_minutes=sync_interval_minutes,
            migration_batch_size=migration_batch_size,
            migration_interval_days=migration_interval_days,
            internal_expiry_months=internal_expiry_months,
            external_expiry_months=external_expiry_months,
            delete_after_months=delete_after_months,
            actor=current.username,
        )
        return RedirectResponse("/settings/synology?saved=1", status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "synology.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/synology/test")
def synology_test(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        message = SynologyService(settings).test_connection()
        return {"ok": True, "message": message}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/synology/sync")
def synology_sync(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        run = SynologyLifecycleService(settings, db).sync(trigger="manual")
        if run.status == "failed":
            return JSONResponse(
                {"ok": False, "error": run.error_message or "Ошибка синхронизации"},
                status_code=503,
            )
        return {
            "ok": True,
            "message": (
                f"Получено учеток: {run.users_count}; новых: {run.new_accounts}; "
                f"требуют действий: {run.planned_actions}; ошибок карточек: {run.detail_errors}."
            ),
        }
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/synology/diagnostics")
def synology_diagnostics(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        result = SynologyService(settings).diagnostics()
        return {
            "ok": True,
            "help": result.help_output,
            "enum": result.enum_output,
            "sample_login": result.sample_login,
            "sample_detail": result.sample_detail,
        }
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/synology/exceptions")
def synology_exception_add(
    request: Request,
    csrf: str = Form(...),
    login: str = Form(...),
    stable_id: str = Form(""),
    reason: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        SynologyLifecycleService(settings, db).add_exception(
            login=login,
            stable_id=stable_id,
            reason=reason,
            actor=current.username,
        )
        return RedirectResponse(
            "/settings/synology?exception_saved=1",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "synology.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/synology/exceptions/{exception_id}/remove")
def synology_exception_remove(
    exception_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        SynologyLifecycleService(settings, db).remove_exception(
            exception_id,
            actor=current.username,
        )
        return RedirectResponse("/settings/synology", status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "synology.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )
