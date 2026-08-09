from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.telegram import TelegramService
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    saved: bool = False,
    error: str = "",
):
    current = require_admin(request)
    service = TelegramService(settings.app_secret_key, db)
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "telegram": service.view(),
        "notifications": service.recent(limit=20),
        "saved": saved,
        "error": error,
    }


@router.get("/settings/telegram")
def telegram_settings_page(
    request: Request,
    saved: int = 0,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "telegram_settings.html",
        _context(
            request,
            settings=settings,
            db=db,
            saved=bool(saved),
        ),
    )


@router.post("/settings/telegram")
def telegram_settings_save(
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(""),
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    topic_id: str = Form(""),
    clear_token: str = Form(""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        TelegramService(settings.app_secret_key, db).save(
            enabled=enabled.strip().lower() in {"1", "true", "yes", "on"},
            bot_token=bot_token,
            chat_id=chat_id,
            topic_id=topic_id,
            operator=current.username,
            clear_token=(
                clear_token.strip().lower() in {"1", "true", "yes", "on"}
            ),
        )
        return RedirectResponse("/settings/telegram?saved=1", status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "telegram_settings.html",
            _context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/telegram/test")
def telegram_settings_test(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        message = TelegramService(
            settings.app_secret_key,
            db,
        ).test_connection(send_test_message=True)
        return {"ok": True, "message": message}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/telegram/retry-failed")
def telegram_retry_failed(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        count = TelegramService(
            settings.app_secret_key,
            db,
        ).retry_failed()
        return {"ok": True, "count": count}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )
