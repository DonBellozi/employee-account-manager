from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.zimbra_protection import ManagedZimbraObserverService
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    message: str = "",
    error: str = "",
):
    current = require_admin(request)
    service = ManagedZimbraObserverService(settings, db)
    protections = service.list_protections(limit=3000)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "protections": protections,
        "active_count": sum(1 for item in protections if item.is_active),
        "inactive_count": sum(1 for item in protections if not item.is_active),
        "migration": service.migration_view(),
        "source_labels": {
            item.source: service.source_label(item.source) for item in protections
        },
        "message": message,
        "error": error,
    }


@router.get("/settings/zimbra-protection")
def protection_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "zimbra_protection.html",
        _context(request, settings=settings, db=db),
    )


@router.post("/settings/zimbra-protection/import")
def protection_import(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        result = ManagedZimbraObserverService(settings, db).import_legacy_never_disable(
            current.username
        )
        message = (
            f"Импорт завершен: найдено – {result['found']}, импортировано – "
            f"{result['imported']}, уже защищены – {result['already_active']}, "
            f"ранее снятая защита не восстановлена – {result['inactive_skipped']}."
        )
        if result["without_id"]:
            message += f" Без zimbraId – {result['without_id']}."
        return templates.TemplateResponse(
            request,
            "zimbra_protection.html",
            _context(request, settings=settings, db=db, message=message),
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_protection.html",
            _context(request, settings=settings, db=db, error=str(exc)),
            status_code=503,
        )


@router.post("/settings/zimbra-protection/add")
def protection_add(
    request: Request,
    email: str = Form(...),
    reason: str = Form(""),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        row = ManagedZimbraObserverService(settings, db).protect_manually(
            email, reason, current.username
        )
        return templates.TemplateResponse(
            request,
            "zimbra_protection.html",
            _context(
                request,
                settings=settings,
                db=db,
                message=f"Защита включена для {row.primary_email}.",
            ),
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_protection.html",
            _context(request, settings=settings, db=db, error=str(exc)),
            status_code=400,
        )


@router.post("/settings/zimbra-protection/{protection_id}/deactivate")
def protection_deactivate(
    protection_id: int,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        row = ManagedZimbraObserverService(settings, db).deactivate(
            protection_id, current.username
        )
        return RedirectResponse("/settings/zimbra-protection", status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_protection.html",
            _context(request, settings=settings, db=db, error=str(exc)),
            status_code=400,
        )


@router.post("/settings/zimbra-protection/{protection_id}/reactivate")
def protection_reactivate(
    protection_id: int,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        ManagedZimbraObserverService(settings, db).reactivate(
            protection_id, current.username
        )
        return RedirectResponse("/settings/zimbra-protection", status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "zimbra_protection.html",
            _context(request, settings=settings, db=db, error=str(exc)),
            status_code=400,
        )
