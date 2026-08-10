from __future__ import annotations

from urllib.parse import quote_plus

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import AuditLog, DomainMailProfile
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.dismissal_mailer import (
    DISMISSAL_TEMPLATE_VARIABLES,
    ensure_dismissal_mail_templates,
)
from app.services.mailer import (
    CORPORATE_TEMPLATE_VARIABLES,
    PERSONAL_TEMPLATE_VARIABLES,
    ensure_domain_mail_profiles,
    validate_mail_template,
)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

TEMPLATE_DEFINITIONS = {
    "personal": {
        "label": "Реквизиты корпоративной почты",
        "short_label": "Реквизиты почты",
        "description": "Отправляется на личный e-mail после создания почтового ящика.",
        "variables": PERSONAL_TEMPLATE_VARIABLES,
    },
    "corporate": {
        "label": "Реквизиты доменной учетной записи",
        "short_label": "Реквизиты AD",
        "description": "Отправляется на корпоративный адрес после создания учетной записи AD.",
        "variables": CORPORATE_TEMPLATE_VARIABLES,
    },
    "dismissal": {
        "label": "Возврат оборудования при увольнении",
        "short_label": "Увольнение / оборудование",
        "description": "Автоматическое уведомление при окончательном увольнении работника.",
        "variables": DISMISSAL_TEMPLATE_VARIABLES,
    },
}


def _redirect(
    *,
    profile_id: int,
    template_key: str,
    message: str = "",
    error: str = "",
) -> RedirectResponse:
    params = [f"selected={profile_id}:{template_key}"]
    if message:
        params.append(f"message={quote_plus(message)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    return RedirectResponse(
        f"/admin/mail-templates?{'&'.join(params)}",
        status_code=303,
    )


def _page_context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
):
    current = require_admin(request)
    profiles = ensure_domain_mail_profiles(db, settings)
    dismissal_templates = ensure_dismissal_mail_templates(db, settings)
    selected = request.query_params.get("selected", "").strip()
    valid_keys = {
        f"{profile.id}:{template_key}"
        for profile in profiles
        for template_key in TEMPLATE_DEFINITIONS
    }
    if selected not in valid_keys:
        selected = (
            f"{profiles[0].id}:personal"
            if profiles
            else ""
        )

    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "profiles": profiles,
        "dismissal_templates": dismissal_templates,
        "template_definitions": TEMPLATE_DEFINITIONS,
        "selected_key": selected,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    }


@router.get("/admin/mail-templates")
def mail_templates(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "admin_mail_templates.html",
        _page_context(
            request,
            settings=settings,
            db=db,
        ),
    )


@router.post("/admin/mail-templates/{profile_id}")
def update_mail_template(
    profile_id: int,
    request: Request,
    template_key: str = Form(...),
    sender_name: str = Form(""),
    sender_email: str = Form(...),
    subject: str = Form(...),
    body_html: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    key = template_key.strip().lower()

    try:
        definition = TEMPLATE_DEFINITIONS.get(key)
        if definition is None:
            raise ValueError("Неизвестный тип шаблона")

        profile = db.get(DomainMailProfile, profile_id)
        if profile is None:
            raise ValueError("Профиль почтового домена не найден")

        configured_domains = {
            domain.strip().lower()
            for domain in settings.zimbra_domains
            if domain.strip()
        }
        if configured_domains and profile.domain.lower() not in configured_domains:
            raise ValueError("Этот домен отсутствует в настройках Zimbra")

        try:
            normalized_sender = validate_email(
                sender_email.strip(),
                check_deliverability=False,
            ).normalized.lower()
        except EmailNotValidError as exc:
            raise ValueError(
                f"Некорректный e-mail отправителя: {exc}"
            ) from exc

        sender_domain = normalized_sender.rsplit("@", 1)[1]
        if sender_domain != profile.domain.strip().lower():
            raise ValueError(
                "E-mail отправителя должен принадлежать домену "
                f"{profile.domain}"
            )

        validate_mail_template(
            subject,
            allowed_variables=set(definition["variables"]),
            field_name="Тема письма",
            autoescape=False,
        )
        validate_mail_template(
            body_html,
            allowed_variables=set(definition["variables"]),
            field_name="HTML-шаблон письма",
            autoescape=True,
        )

        profile.sender_name = sender_name.strip()
        profile.sender_email = normalized_sender
        profile.updated_by = current.username

        if key == "personal":
            profile.personal_subject = subject.strip()
            profile.personal_body_html = body_html.strip()
        elif key == "corporate":
            profile.corporate_subject = subject.strip()
            profile.corporate_body_html = body_html.strip()
        else:
            dismissal_templates = ensure_dismissal_mail_templates(
                db,
                settings,
            )
            dismissal_template = dismissal_templates.get(
                profile.domain.strip().lower()
            )
            if dismissal_template is None:
                raise ValueError("Шаблон увольнения для домена не найден")
            dismissal_template.subject = subject.strip()
            dismissal_template.body_html = body_html.strip()
            dismissal_template.updated_by = current.username

        db.add(
            AuditLog(
                actor=current.username,
                action="mail_template_update",
                target=f"{profile.domain}:{key}",
                result="success",
                details=f"sender={normalized_sender}",
            )
        )
        db.commit()
        return _redirect(
            profile_id=profile.id,
            template_key=key,
            message=f"Шаблон «{definition['label']}» сохранен",
        )
    except Exception as exc:
        db.rollback()
        return _redirect(
            profile_id=profile_id,
            template_key=key or "personal",
            error=str(exc),
        )
