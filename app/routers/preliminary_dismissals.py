from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.preliminary_dismissals import PreliminaryDismissalService
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    saved: bool = False,
    result: str = "",
    error: str = "",
):
    current = require_admin(request)
    service = PreliminaryDismissalService(settings, db)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "summary": service.summary(),
        "sources": service.source_options(),
        "saved": saved,
        "result": result,
        "error": error,
    }


@router.get("/settings/preliminary-dismissals")
def settings_page(
    request: Request,
    saved: int = 0,
    result: str = "",
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "preliminary_dismissals.html",
        _context(
            request,
            settings=settings,
            db=db,
            saved=bool(saved),
            result=result,
        ),
    )


@router.post("/settings/preliminary-dismissals/save")
def save_settings(
    request: Request,
    csrf: str = Form(...),
    source_id: str = Form(""),
    imap_folder: str = Form("INBOX"),
    sender_filter: str = Form(""),
    subject_filter: str = Form(""),
    enabled: str = Form(""),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        PreliminaryDismissalService(settings, db).save_settings(
            enabled=enabled.strip().casefold() in {"1", "true", "yes", "on"},
            source_id=source_id,
            imap_folder=imap_folder,
            sender_filter=sender_filter,
            subject_filter=subject_filter,
            operator=current.username,
        )
        return RedirectResponse(
            "/settings/preliminary-dismissals?saved=1",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "preliminary_dismissals.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/preliminary-dismissals/check-now")
def check_now(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        result = PreliminaryDismissalService(settings, db).process(force=True)
        message = (
            f"Писем: {int(result.get('messages', 0) or 0)}, "
            f"работников: {int(result.get('items', 0) or 0)}, "
            f"сопоставлено: {int(result.get('matched', 0) or 0)}."
        )
        return RedirectResponse(
            "/settings/preliminary-dismissals?result=" + quote_plus(message),
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "preliminary_dismissals.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=503,
        )
