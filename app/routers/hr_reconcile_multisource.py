from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import require_admin, validate_csrf
from app.services.hr_registry_multisource import (
    MultiSourceHRRegistryViewService,
)


router = APIRouter()


@router.post("/settings/onec/reconcile")
def reconcile_all_sources(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        result = MultiSourceHRRegistryViewService(
            settings,
            db,
        ).reconcile_all()
        return {
            "ok": True,
            "summary": result["summary"],
            "sources": result["sources"],
            "errors": result["errors"],
        }
    except Exception as exc:
        db.rollback()
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )
