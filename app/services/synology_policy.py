from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date


CLASS_INTERNAL_ACTIVE = "internal_active"
CLASS_INTERNAL_DISMISSED = "internal_dismissed"
CLASS_EXTERNAL = "external"
CLASS_UNKNOWN = "unknown"
CLASS_EXCEPTION = "exception"
CLASS_PROTECTED = "protected_system"

ACTION_NONE = "none"
ACTION_CLASSIFY = "classify"
ACTION_MIGRATION_CANDIDATE = "migration_candidate"
ACTION_SET_EXPIRY_INTERNAL = "set_expiry_internal"
ACTION_SET_EXPIRY_EXTERNAL = "set_expiry_external"
ACTION_DISABLE = "disable"
ACTION_DELETE = "delete"


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str


def normalize_login(value: str) -> str:
    return str(value or "").strip().casefold()


def normalize_domain(value: str) -> str:
    return str(value or "").strip().lower().rstrip(".")


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def email_domain(value: str) -> str:
    email = normalize_email(value)
    if email.count("@") != 1:
        return ""
    local, domain = email.rsplit("@", 1)
    if not local or not domain:
        return ""
    return normalize_domain(domain)


def add_months(value: date, months: int) -> date:
    months = max(0, int(months))
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def classify_account(
    *,
    email: str,
    managed_domains: set[str],
    protected: bool,
    exception: bool,
    active_employee: bool,
) -> str:
    if protected:
        return CLASS_PROTECTED
    if exception:
        return CLASS_EXCEPTION

    domain = email_domain(email)
    if not domain:
        return CLASS_UNKNOWN

    normalized_domains = {normalize_domain(item) for item in managed_domains if item}
    if domain in normalized_domains:
        # Согласованное правило проекта: учетная запись нашего домена считается
        # уволенной, если ее email отсутствует среди действующих работников всех
        # кадровых источников. Наличие worker_key для этого не обязательно.
        return CLASS_INTERNAL_ACTIVE if active_employee else CLASS_INTERNAL_DISMISSED
    return CLASS_EXTERNAL


def desired_action(
    *,
    classification: str,
    is_active: bool,
    observed_expires_at: date | None,
    today: date,
    delete_after: date | None,
    enrolled: bool,
    policy_expires_at: date | None,
    previous_active: bool | None,
    internal_months: int,
    external_months: int,
) -> PolicyDecision:
    if classification in {CLASS_EXCEPTION, CLASS_PROTECTED}:
        return PolicyDecision(ACTION_NONE, "Автоматизация отключена для этой учетки.")

    if classification == CLASS_UNKNOWN:
        return PolicyDecision(
            ACTION_CLASSIFY,
            "Нет надежных данных для автоматической классификации.",
        )

    if classification == CLASS_INTERNAL_DISMISSED:
        if delete_after is not None and delete_after <= today:
            return PolicyDecision(
                ACTION_DELETE,
                f"Срок хранения после увольнения истек {delete_after.isoformat()}.",
            )
        if is_active:
            return PolicyDecision(
                ACTION_DISABLE,
                "Учетка нашего домена отсутствует среди действующих работников.",
            )
        return PolicyDecision(ACTION_NONE, "Уволенная локальная учетка уже отключена.")

    if classification == CLASS_EXTERNAL:
        if not is_active:
            return PolicyDecision(
                ACTION_NONE,
                "Внешняя учетка неактивна; автоматически не включаем ее.",
            )
        limit = add_months(today, external_months)
        if observed_expires_at is None:
            return PolicyDecision(
                ACTION_SET_EXPIRY_EXTERNAL,
                f"Внешняя учетка не должна быть бессрочной; максимум {external_months} мес.",
            )
        if observed_expires_at > limit:
            return PolicyDecision(
                ACTION_SET_EXPIRY_EXTERNAL,
                f"Срок внешней учетки превышает максимум {external_months} мес.",
            )
        return PolicyDecision(ACTION_NONE, "Срок внешней учетки находится в пределах политики.")

    if classification == CLASS_INTERNAL_ACTIVE:
        if not enrolled:
            return PolicyDecision(
                ACTION_MIGRATION_CANDIDATE,
                f"Кандидат на постепенную миграцию локальной учетки: {internal_months} мес.",
            )
        if not is_active:
            return PolicyDecision(
                ACTION_NONE,
                "Локальная учетка сотрудника отключена; ожидаем увольнение или ручную реактивацию.",
            )
        if previous_active is False:
            return PolicyDecision(
                ACTION_SET_EXPIRY_INTERNAL,
                f"Обнаружена реактивация; требуется новый срок {internal_months} мес.",
            )
        if observed_expires_at is None:
            return PolicyDecision(
                ACTION_SET_EXPIRY_INTERNAL,
                f"Срок снят; требуется восстановить ограничение на {internal_months} мес.",
            )
        if policy_expires_at is not None and observed_expires_at > policy_expires_at:
            return PolicyDecision(
                ACTION_SET_EXPIRY_INTERNAL,
                f"Срок продлен сверх текущего цикла; требуется новый цикл {internal_months} мес.",
            )
        return PolicyDecision(ACTION_NONE, "Текущий трехмесячный цикл не требует изменения.")

    return PolicyDecision(ACTION_CLASSIFY, "Неизвестное lifecycle-состояние.")
