from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.onec_additional_import import OneCAdditionalImportService
from app.services.onec_sources import OneCSourceRegistryService
from app.services.zimbra_lifecycle import ZimbraLifecycleService
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    saved: bool = False,
    result: dict | None = None,
    error: str = "",
):
    current = require_admin(request)
    service = OneCSourceRegistryService(settings, db)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "primary": service.primary_source(),
        "sources": service.page_rows(),
        "saved": saved,
        "result": result,
        "error": error,
    }


@router.get("/settings/onec-sources")
def source_page(
    request: Request,
    saved: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "onec_sources.html",
        _context(
            request,
            settings=settings,
            db=db,
            saved=bool(saved),
        ),
    )


@router.post("/settings/onec-sources/save")
def source_save(
    request: Request,
    csrf: str = Form(...),
    source_id: int | None = Form(None),
    name: str = Form(...),
    mail_domain: str = Form(...),
    sender_filter: str = Form(...),
    attachment_filename: str = Form(...),
    has_corporate_email: str = Form(""),
    enabled: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    truthy = {"1", "true", "yes", "on"}
    try:
        saved_source = OneCSourceRegistryService(settings, db).save(
            source_id=source_id,
            name=name,
            mail_domain=mail_domain,
            sender_filter=sender_filter,
            attachment_filename=attachment_filename,
            has_corporate_email=(
                has_corporate_email.strip().lower() in truthy
            ),
            enabled=enabled.strip().lower() in truthy,
            operator=current.username,
        )
        if saved_source.enabled and not saved_source.has_corporate_email:
            lifecycle = ZimbraLifecycleService(settings, db)
            current_lifecycle = lifecycle.get_settings_record()
            if (
                current_lifecycle.allow_close
                or current_lifecycle.allow_backup
                or current_lifecycle.allow_delete
            ):
                lifecycle.save_settings(
                    allow_close=False,
                    allow_backup=False,
                    allow_delete=False,
                    backup_dir=current_lifecycle.backup_dir,
                    operator=current.username,
                )
        return RedirectResponse(
            "/settings/onec-sources?saved=1",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "onec_sources.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/onec-sources/{source_id}/toggle")
def source_toggle(
    source_id: int,
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    OneCSourceRegistryService(settings, db).set_enabled(
        source_id,
        enabled=enabled.strip().lower() in {"1", "true", "yes", "on"},
        operator=current.username,
    )
    return RedirectResponse("/settings/onec-sources", status_code=303)


@router.post("/settings/onec-sources/{source_id}/import")
def source_import(
    source_id: int,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    require_admin(request)
    registry = OneCSourceRegistryService(settings, db)
    try:
        source = registry.get(source_id)
        result = OneCAdditionalImportService(
            settings,
            db,
            source,
        ).analyze_latest(trigger="manual")
        return templates.TemplateResponse(
            request,
            "onec_sources.html",
            _context(
                request,
                settings=settings,
                db=db,
                result=result,
            ),
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "onec_sources.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=503,
        )
