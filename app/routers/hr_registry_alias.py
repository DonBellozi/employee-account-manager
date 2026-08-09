from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.hr_registry_alias import HRRegistryAliasService
from app.services.hr_registry_multisource import (
    MultiSourceHRRegistryViewService,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(
    Jinja2Templates(directory="app/templates")
)


def _context(
    request: Request,
    *,
    record_id: int,
    settings: Settings,
    db: Session,
    error: str = "",
    embedded: bool = False,
    saved: bool = False,
    result: dict | None = None,
):
    current = require_admin(request)
    service = HRRegistryAliasService(settings, db)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "item": service.plan(record_id),
        "error": error,
        "embedded": embedded,
        "saved": saved,
        "result": result,
    }


@router.get("/employees/registry/{record_id}/alias")
def alias_page(
    record_id: int,
    request: Request,
    modal: int = 0,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "hr_registry_alias.html",
        _context(
            request,
            record_id=record_id,
            settings=settings,
            db=db,
            embedded=bool(modal),
        ),
    )


@router.post("/employees/registry/{record_id}/alias")
def alias_create(
    record_id: int,
    request: Request,
    csrf: str = Form(...),
    modal: str = Form("0"),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    embedded = modal.strip().lower() in {"1", "true", "yes", "on"}

    try:
        result = HRRegistryAliasService(
            settings,
            db,
        ).create_or_bind(
            record_id=record_id,
            actor=current.username,
        )

        if not result.get("dry_run"):
            MultiSourceHRRegistryViewService(
                settings,
                db,
            ).reconcile_all()

        if embedded:
            return templates.TemplateResponse(
                request,
                "hr_registry_alias.html",
                _context(
                    request,
                    record_id=record_id,
                    settings=settings,
                    db=db,
                    embedded=True,
                    saved=not result.get("dry_run", False),
                    result=result,
                ),
            )

        return RedirectResponse(
            "/employees/registry",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "hr_registry_alias.html",
            _context(
                request,
                record_id=record_id,
                settings=settings,
                db=db,
                error=str(exc),
                embedded=embedded,
            ),
            status_code=400,
        )
