from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.ad import ActiveDirectoryService
from app.services.mailer import (
    CredentialMailer,
    ensure_domain_mail_profiles,
    get_domain_mail_profile,
    render_mail_template,
)
from app.services.techexpert_settings import (
    TECHEXPERT_TEMPLATE_VARIABLES,
    TechExpertSettingsService,
    build_techexpert_template_context,
    normalize_email,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))


def _redirect(*, message: str = "", error: str = "") -> RedirectResponse:
    parts = []
    if message:
        parts.append(f"message={quote_plus(message)}")
    if error:
        parts.append(f"error={quote_plus(error)}")
    suffix = f"?{'&'.join(parts)}" if parts else ""
    return RedirectResponse(f"/settings/techexpert{suffix}", status_code=303)


def _page_context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
    error: str = "",
) -> dict[str, object]:
    current = require_admin(request)
    service = TechExpertSettingsService(settings, db)
    config = service.get()
    profiles = {
        profile.domain.strip().lower(): profile
        for profile in ensure_domain_mail_profiles(db, settings)
    }
    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "techexpert": config,
        "source_domains": service.available_domains(),
        "sender_profile": profiles.get(config.source_domain.strip().lower()),
        "template_variables": TECHEXPERT_TEMPLATE_VARIABLES,
        "smtp_configured": bool(settings.smtp_host),
        "app_timezone": settings.app_timezone,
        "message": request.query_params.get("message", ""),
        "error": error or request.query_params.get("error", ""),
    }


@router.get("/settings/techexpert")
def techexpert_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "techexpert.html",
        _page_context(request, settings=settings, db=db),
    )


@router.post("/settings/techexpert")
def techexpert_save(
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(""),
    source_domain: str = Form(...),
    ad_group_dn: str = Form(...),
    recipient_email: str = Form(...),
    notification_time: str = Form(...),
    subject: str = Form(...),
    body_html: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    try:
        TechExpertSettingsService(settings, db).save(
            enabled=enabled.strip().lower() in {"1", "true", "yes", "on"},
            source_domain=source_domain,
            ad_group_dn=ad_group_dn,
            recipient_email=recipient_email,
            notification_time=notification_time,
            subject=subject,
            body_html=body_html,
            actor=current.username,
        )
        return _redirect(message="Настройки Техэксперта сохранены")
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "techexpert.html",
            _page_context(
                request,
                settings=settings,
                db=db,
                error=str(exc),
            ),
            status_code=400,
        )


@router.post("/settings/techexpert/check")
def techexpert_check(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        if not config.ad_group_dn.strip():
            raise ValueError("Сначала сохраните DN маркерной группы AD")
        ActiveDirectoryService(settings).test_group(config.ad_group_dn)
        CredentialMailer(settings).test_connection()
        return _redirect(
            message="Группа AD найдена, SMTP-подключение работает"
        )
    except Exception as exc:
        return _redirect(error=f"Проверка не пройдена: {exc}")


@router.post("/settings/techexpert/test-email")
def techexpert_test_email(
    request: Request,
    csrf: str = Form(...),
    test_recipient: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)
    try:
        config = TechExpertSettingsService(settings, db).get()
        recipient = normalize_email(
            test_recipient,
            field_name="тестовый e-mail",
        )
        profile = get_domain_mail_profile(
            db,
            settings,
            config.source_domain,
        )
        context = build_techexpert_template_context(
            [
                {
                    "full_name": "Иванов Иван Иванович",
                    "corporate_email": f"ivanov@{config.source_domain}",
                    "organization": "Тестовая организация",
                    "dismissal_date": "20.08.2026",
                },
                {
                    "full_name": "Петрова Анна Сергеевна",
                    "corporate_email": f"petrova@{config.source_domain}",
                    "organization": "Тестовая организация",
                    "dismissal_date": "20.08.2026",
                },
            ]
        )
        CredentialMailer(settings).send_html(
            recipient=recipient,
            subject=render_mail_template(
                config.subject,
                context,
                autoescape=False,
            ),
            body_html=render_mail_template(
                config.body_html,
                context,
                autoescape=True,
            ),
            sender_email=profile.sender_email,
            sender_name=profile.sender_name,
        )
        return _redirect(message=f"Тестовое письмо отправлено на {recipient}")
    except Exception as exc:
        return _redirect(error=f"Тестовое письмо не отправлено: {exc}")
