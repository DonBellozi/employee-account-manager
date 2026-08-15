from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import (
    CurrentUser,
    authenticate_local,
    get_domain_access,
    get_or_create_csrf,
    normalize_username,
    validate_csrf,
)
from app.services.ad import ActiveDirectoryService
from app.time_utils import register_datetime_filters

router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))
logger = logging.getLogger(__name__)


@router.get("/login")
def login_page(request: Request, settings: Settings = Depends(get_settings)):
    error = ""
    if request.query_params.get("csrf_error") == "1":
        error = "Сессия формы устарела или cookie не была сохранена. Войдите еще раз."

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "csrf": get_or_create_csrf(request),
            "error": error,
            "auth_mode": settings.auth_mode,
        },
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    normalized_username = normalize_username(username)
    current: CurrentUser | None = None

    if settings.auth_mode in {"local", "hybrid"}:
        current = authenticate_local(db, normalized_username, password)

    if current is None and settings.auth_mode in {"ad", "hybrid"}:
        if not settings.ad_login_enabled:
            logger.warning(
                "Вход AD отклонен для %s: AD_LOGIN_ENABLED=false",
                normalized_username,
            )
        else:
            access = get_domain_access(db, normalized_username)
            if access is None:
                logger.warning(
                    "Вход AD отклонен для %s: активный доступ в админке не назначен",
                    normalized_username,
                )
            elif ActiveDirectoryService(settings).authenticate_operator(
                normalized_username,
                password,
            ):
                current = CurrentUser(
                    username=access.username,
                    role=access.role.value,
                    source="ad",
                )
            else:
                logger.warning(
                    "Вход AD отклонен для %s: AD не подтвердил пароль или учетная запись отключена",
                    normalized_username,
                )

    if current is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf": get_or_create_csrf(request),
                "error": "Неверный логин, пароль или доступ к системе не назначен",
                "auth_mode": settings.auth_mode,
            },
            status_code=401,
        )

    request.session.pop("csrf", None)
    request.session["user"] = {
        "username": current.username,
        "role": current.role,
        "source": current.source,
    }
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    validate_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
