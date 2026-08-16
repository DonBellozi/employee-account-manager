from __future__ import annotations

import re
from datetime import time

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models_onec_sources import OneCAdditionalSource
from app.models_techexpert import TechExpertSettings
from app.services.mailer import validate_mail_template


TECHEXPERT_TEMPLATE_VARIABLES = {
    "full_name",
    "corporate_email",
    "organization",
    "dismissal_date",
}

DEFAULT_TECHEXPERT_SUBJECT = (
    "Прекращение доступа к системе «Техэксперт»: {{ full_name }}"
)

DEFAULT_TECHEXPERT_BODY_HTML = """\
<p>Здравствуйте!</p>
<p>Просим прекратить доступ к системе «Техэксперт» для работника:</p>
<p>
  <strong>ФИО:</strong> {{ full_name }}<br>
  <strong>Корпоративный e-mail:</strong> {{ corporate_email }}<br>
  <strong>Организация:</strong> {{ organization }}
</p>
<p>Это автоматическое уведомление по подтвержденному кадровому событию.</p>
"""


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


def parse_notification_time(value: str) -> time:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{2}):(\d{2})", text)
    if not match:
        raise ValueError("Время отправки должно быть в формате ЧЧ:ММ")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("Указано недопустимое время отправки")
    return time(hour, minute)


def normalize_email(value: str, *, field_name: str) -> str:
    try:
        return validate_email(
            str(value or "").strip(),
            check_deliverability=False,
        ).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError(f"Некорректный {field_name}: {exc}") from exc


def ensure_techexpert_settings(db: Session) -> TechExpertSettings:
    row = db.get(TechExpertSettings, 1)
    if row is not None:
        return row
    row = TechExpertSettings(
        id=1,
        enabled=False,
        notification_time="08:45",
        subject=DEFAULT_TECHEXPERT_SUBJECT,
        body_html=DEFAULT_TECHEXPERT_BODY_HTML,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TechExpertSettingsService:
    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def get(self) -> TechExpertSettings:
        return ensure_techexpert_settings(self.db)

    def available_domains(self) -> list[str]:
        mail_domains = {
            normalize(domain)
            for domain in self.settings.zimbra_domains
            if normalize(domain)
        }
        hr_domains = {
            normalize(value)
            for value in self.db.scalars(
                select(OneCAdditionalSource.mail_domain).where(
                    OneCAdditionalSource.enabled.is_(True)
                )
            ).all()
            if normalize(value)
        }
        if hr_domains:
            return sorted(mail_domains.intersection(hr_domains))
        return sorted(mail_domains)

    def validate(
        self,
        *,
        source_domain: str,
        ad_group_dn: str,
        recipient_email: str,
        notification_time: str,
        subject: str,
        body_html: str,
    ) -> dict[str, str]:
        domain = normalize(source_domain)
        if not domain:
            raise ValueError("Выберите организацию Техэксперта")
        if domain not in self.available_domains():
            raise ValueError(
                "Выбранная организация отсутствует среди кадровых/почтовых доменов"
            )
        group_dn = str(ad_group_dn or "").strip()
        if not group_dn:
            raise ValueError("Укажите DN маркерной группы AD")
        recipient = normalize_email(
            recipient_email,
            field_name="e-mail получателя",
        )
        parsed_time = parse_notification_time(notification_time)
        normalized_time = f"{parsed_time.hour:02d}:{parsed_time.minute:02d}"

        validate_mail_template(
            subject,
            allowed_variables=TECHEXPERT_TEMPLATE_VARIABLES,
            field_name="Тема письма",
            autoescape=False,
        )
        validate_mail_template(
            body_html,
            allowed_variables=TECHEXPERT_TEMPLATE_VARIABLES,
            field_name="HTML-шаблон письма",
            autoescape=True,
        )
        return {
            "source_domain": domain,
            "ad_group_dn": group_dn,
            "recipient_email": recipient,
            "notification_time": normalized_time,
            "subject": subject.strip(),
            "body_html": body_html.strip(),
        }

    def save(
        self,
        *,
        enabled: bool,
        source_domain: str,
        ad_group_dn: str,
        recipient_email: str,
        notification_time: str,
        subject: str,
        body_html: str,
        actor: str,
    ) -> TechExpertSettings:
        values = self.validate(
            source_domain=source_domain,
            ad_group_dn=ad_group_dn,
            recipient_email=recipient_email,
            notification_time=notification_time,
            subject=subject,
            body_html=body_html,
        )
        row = self.get()
        row.enabled = bool(enabled)
        row.source_domain = values["source_domain"]
        row.ad_group_dn = values["ad_group_dn"]
        row.recipient_email = values["recipient_email"]
        row.notification_time = values["notification_time"]
        row.subject = values["subject"]
        row.body_html = values["body_html"]
        row.updated_by = str(actor or "").strip()
        self.db.commit()
        self.db.refresh(row)
        return row
