from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models_mail_templates import DismissalMailTemplate
from app.services.mailer import (
    CredentialMailer,
    ensure_domain_mail_profiles,
    render_mail_template,
)


DISMISSAL_TEMPLATE_VARIABLES = {
    "full_name",
    "dismissal_date",
    "return_deadline",
    "return_deadline_text",
    "organization",
    "organizations",
    "corporate_email",
    "personal_email",
}

DEFAULT_DISMISSAL_SUBJECT = "Возврат оборудования при увольнении"
DEFAULT_DISMISSAL_BODY_HTML = """\
<p>Уважаемый(ая) {{ full_name }}!</p>
<p>
  Согласно информации, поступившей из Кадрового департамента, Вы приняли
  решение покинуть наш коллектив. Дата увольнения –
  <strong>{{ dismissal_date }}</strong>.
</p>
<p>
  Просим Вас вернуть выданное Вам для выполнения служебных обязанностей
  оборудование, принадлежащее организации, в Отдел информатизации и
  автоматизации <strong>{{ return_deadline_text }}</strong>.
</p>
<p>
  Если выданное оборудование уже возвращено, дополнительных действий
  не требуется.
</p>
<p>Спасибо за сотрудничество и успехов в дальнейших начинаниях!</p>
"""


def ensure_dismissal_mail_templates(
    db: Session,
    settings: Settings,
) -> dict[str, DismissalMailTemplate]:
    profiles = ensure_domain_mail_profiles(db, settings)
    domains = [profile.domain.strip().lower() for profile in profiles]
    existing = {
        row.domain.strip().lower(): row
        for row in db.scalars(
            select(DismissalMailTemplate).where(
                DismissalMailTemplate.domain.in_(domains)
            )
        ).all()
    } if domains else {}

    changed = False
    for domain in domains:
        if domain in existing:
            continue
        row = DismissalMailTemplate(
            domain=domain,
            subject=DEFAULT_DISMISSAL_SUBJECT,
            body_html=DEFAULT_DISMISSAL_BODY_HTML,
        )
        db.add(row)
        existing[domain] = row
        changed = True

    if changed:
        db.commit()

    return existing


def get_dismissal_mail_template(
    db: Session,
    settings: Settings,
    domain: str,
) -> DismissalMailTemplate:
    normalized = str(domain or "").strip().lower()
    templates = ensure_dismissal_mail_templates(db, settings)
    template = templates.get(normalized)
    if template is None:
        raise RuntimeError(
            f"Для почтового домена {normalized} не настроен шаблон увольнения"
        )
    return template


class DismissalMailer(CredentialMailer):
    def send_notice(
        self,
        *,
        template: DismissalMailTemplate,
        sender_email: str,
        sender_name: str,
        recipient: str,
        context: dict[str, str],
    ) -> None:
        subject = render_mail_template(
            template.subject,
            context,
            autoescape=False,
        )
        body = render_mail_template(
            template.body_html,
            context,
            autoescape=True,
        )
        self._send(
            recipient,
            subject,
            body,
            sender_email=sender_email,
            sender_name=sender_name,
        )
