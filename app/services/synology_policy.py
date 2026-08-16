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
# Имена сохранены для совместимости с существующей БД/UI. На этапе блокировки
# даты 3/6 месяцев хранятся только в SQLite; DSM получает только Expired=true
# после наступления policy_expires_at.
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
    matched_employee: bool = False,
) -> str:
    """Определить класс локальной учетной записи DSM.

    Порядок фильтров критичен и выстроен от самого безопасного к самому
    решительному:

    1. системная запись DSM — не трогаем никогда;
    2. человек найден в кадровой выгрузке и работает — не трогаем как
       уволенного (он попадет в обычный миграционный цикл);
    3. список исключений — не трогаем;
    4. только потом домен, увольнение и внешние доступы.

    Пункт 2 стоит выше остальных намеренно. Раньше присутствие человека
    проверялось лишь косвенно, через совпадение корпоративной почты, и
    действующие работники с незаполненным или личным адресом в DSM
    выглядели как уволенные. Это привело к их ошибочной блокировке.
    """
    if protected:
        return CLASS_PROTECTED

    # Первый уровень фильтрации: человек есть в выгрузке и работает.
    if active_employee:
        return CLASS_INTERNAL_ACTIVE

    if exception:
        return CLASS_EXCEPTION

    domain = email_domain(email)
    if not domain:
        # Ни одного признака связи с работником и нет домена: данных для
        # автоматического решения недостаточно. Такая запись не блокируется.
        return CLASS_UNKNOWN

    normalized_domains = {normalize_domain(item) for item in managed_domains if item}
    if domain in normalized_domains:
        # Уволенным считается только тот, кого удалось однозначно сопоставить
        # с кадровыми данными и он там уже не работает. Несопоставленная
        # учетка нашего домена — это пробел в данных, а не увольнение.
        if matched_employee:
            return CLASS_INTERNAL_DISMISSED
        return CLASS_UNKNOWN
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
    # observed_expires_at/delete_after оставлены в сигнатуре для совместимости.
    # На текущем этапе DSM не хранит календарный срок и удаление не выполняется.
    _ = observed_expires_at, delete_after, previous_active

    if classification in {CLASS_EXCEPTION, CLASS_PROTECTED}:
        return PolicyDecision(ACTION_NONE, "Автоматизация отключена для этой учетки.")

    if classification == CLASS_UNKNOWN:
        # НЕ БЛОКИРОВАТЬ. Попытка отключать нераспознанные учетки привела к
        # инциденту: у действующих работников в DSM часто просто не заполнен
        # e-mail, и они выглядели так же, как бесхозные записи. Отсутствие
        # сопоставления означает недостаток данных, а не отсутствие человека,
        # и трактуется в пользу работника.
        return PolicyDecision(
            ACTION_CLASSIFY,
            "Учетка не сопоставлена с работником. Требуется ручное решение: "
            "автоматическая блокировка по одному лишь отсутствию данных запрещена.",
        )

    if classification == CLASS_INTERNAL_DISMISSED:
        # Увольнение — не зона ответственности этого контура. Решение о том,
        # что человек уволен окончательно, принимается один раз в общем
        # контуре по кадровым данным, и он же блокирует AD, Zimbra и DSM
        # одним прогоном. Дублирующая логика здесь приводила к тому, что
        # Synology самостоятельно «догадывался» об увольнении по одному лишь
        # несовпадению почты и отключал действующих работников.
        return PolicyDecision(
            ACTION_NONE,
            "Увольнение обрабатывается общим контуром вместе с AD и Zimbra.",
        )

    if classification == CLASS_EXTERNAL:
        if not is_active:
            return PolicyDecision(
                ACTION_NONE,
                "Внешняя учетка неактивна; автоматически не включаем ее.",
            )
        if not enrolled or policy_expires_at is None:
            return PolicyDecision(
                ACTION_SET_EXPIRY_EXTERNAL,
                f"Требуется начать цикл контроля на {external_months} мес.",
            )
        if policy_expires_at <= today:
            return PolicyDecision(
                ACTION_DISABLE,
                f"Истек {external_months}-месячный срок контроля ({policy_expires_at.isoformat()}).",
            )
        return PolicyDecision(
            ACTION_NONE,
            f"Цикл действует до {policy_expires_at.isoformat()}.",
        )

    if classification == CLASS_INTERNAL_ACTIVE:
        if not is_active:
            return PolicyDecision(
                ACTION_NONE,
                "Локальная учетка сотрудника уже отключена; автоматически не включаем ее.",
            )
        if not enrolled or policy_expires_at is None:
            return PolicyDecision(
                ACTION_MIGRATION_CANDIDATE,
                f"Кандидат на постепенный цикл блокировки: {internal_months} мес.",
            )
        if policy_expires_at <= today:
            return PolicyDecision(
                ACTION_DISABLE,
                f"Истек {internal_months}-месячный срок контроля ({policy_expires_at.isoformat()}).",
            )
        return PolicyDecision(
            ACTION_NONE,
            f"Цикл действует до {policy_expires_at.isoformat()}.",
        )

    return PolicyDecision(ACTION_CLASSIFY, "Неизвестное lifecycle-состояние.")
