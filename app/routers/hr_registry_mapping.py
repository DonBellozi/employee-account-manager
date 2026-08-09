from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.hr_registry_manual_mapping import (
    HRRegistryManualMappingService,
)
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
):
    current = require_admin(request)
    service = HRRegistryManualMappingService(settings, db)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "item": service.page_data(record_id),
        "error": error,
    }


@router.get("/employees/registry/{record_id}/map")
def mapping_page(
    record_id: int,
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "hr_registry_mapping.html",
        _context(
            request,
            record_id=record_id,
            settings=settings,
            db=db,
        ),
    )


@router.post("/employees/registry/{record_id}/map")
def mapping_save(
    record_id: int,
    request: Request,
    identifier: str = Form(...),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        service = HRRegistryManualMappingService(settings, db)
        result = service.save_identifier(
            record_id=record_id,
            identifier=identifier,
            actor=current.username,
        )
        MultiSourceHRRegistryViewService(
            settings,
            db,
        ).reconcile_all()

        return RedirectResponse(
            "/employees/registry?"
            + urlencode(
                {
                    "q": result["fio"],
                    "source": result["source_id"],
                }
            ),
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "hr_registry_mapping.html",
            _context(
                request,
                record_id=record_id,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/employees/registry/{record_id}/map/delete")
def mapping_delete(
    record_id: int,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    service = HRRegistryManualMappingService(settings, db)
    record = service.get_record(record_id)
    fio = record.fio
    source_id = record.source_id
    service.delete_for_record(
        record_id=record_id,
        actor=current.username,
    )
    MultiSourceHRRegistryViewService(
        settings,
        db,
    ).reconcile_all()

    return RedirectResponse(
        "/employees/registry?"
        + urlencode({"q": fio, "source": source_id}),
        status_code=303,
    )
