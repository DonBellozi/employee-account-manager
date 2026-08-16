from datetime import date

from app.services.synology_policy import (
    ACTION_CLASSIFY,
    ACTION_DELETE,
    ACTION_DISABLE,
    ACTION_MIGRATION_CANDIDATE,
    ACTION_NONE,
    ACTION_SET_EXPIRY_EXTERNAL,
    ACTION_SET_EXPIRY_INTERNAL,
    CLASS_EXCEPTION,
    CLASS_EXTERNAL,
    CLASS_INTERNAL_ACTIVE,
    CLASS_INTERNAL_DISMISSED,
    CLASS_PROTECTED,
    CLASS_UNKNOWN,
    add_months,
    classify_account,
    desired_action,
)


def test_add_months_is_calendar_safe():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2026, 8, 15), 6) == date(2027, 2, 15)


def test_managed_domain_is_active_only_when_worker_is_active():
    domains = {"corp.ru", "second.ru"}
    assert classify_account(
        email="User@CORP.RU",
        managed_domains=domains,
        protected=False,
        exception=False,
        active_employee=True,
    ) == CLASS_INTERNAL_ACTIVE
    # Уволенной считается только учетка, которую удалось связать с человеком,
    # и он больше не работает. Требование matched_employee появилось после
    # инцидента: без него любая несопоставленная учетка нашего домена
    # выглядела как увольнение.
    assert classify_account(
        email="former@corp.ru",
        managed_domains=domains,
        protected=False,
        exception=False,
        active_employee=False,
        matched_employee=True,
    ) == CLASS_INTERNAL_DISMISSED

    assert classify_account(
        email="former@corp.ru",
        managed_domains=domains,
        protected=False,
        exception=False,
        active_employee=False,
        matched_employee=False,
    ) == CLASS_UNKNOWN


def test_classification_precedence_and_external_unknown():
    assert classify_account(
        email="person@external.test",
        managed_domains={"corp.ru"},
        protected=False,
        exception=False,
        active_employee=False,
    ) == CLASS_EXTERNAL
    assert classify_account(
        email="",
        managed_domains={"corp.ru"},
        protected=False,
        exception=False,
        active_employee=False,
    ) == CLASS_UNKNOWN
    assert classify_account(
        email="admin@corp.ru",
        managed_domains={"corp.ru"},
        protected=True,
        exception=False,
        active_employee=False,
    ) == CLASS_PROTECTED
    assert classify_account(
        email="service@corp.ru",
        managed_domains={"corp.ru"},
        protected=False,
        exception=True,
        active_employee=False,
    ) == CLASS_EXCEPTION


def test_dismissal_is_delegated_to_the_shared_contour():
    # Решение об увольнении принимается один раз по кадровым данным, и общий
    # контур блокирует AD, Zimbra и DSM одним прогоном. Собственная логика
    # увольнения здесь приводила к отключению действующих работников.
    for active in (True, False):
        decision = desired_action(
            classification=CLASS_INTERNAL_DISMISSED,
            is_active=active,
            observed_expires_at=None,
            today=date(2026, 8, 11),
            delete_after=date(2027, 2, 11),
            enrolled=False,
            policy_expires_at=None,
            previous_active=None,
            internal_months=3,
            external_months=6,
        )
        assert decision.action == ACTION_NONE
        assert decision.action != ACTION_DISABLE
        assert decision.action != ACTION_DELETE


def test_external_never_becomes_unlimited():
    base = dict(
        classification=CLASS_EXTERNAL,
        is_active=True,
        today=date(2026, 8, 11),
        delete_after=None,
        previous_active=True,
        internal_months=3,
        external_months=6,
    )
    # Срок контроля хранится приложением, а не DSM: наблюдаемая в DSM дата
    # больше не влияет на решение.
    assert desired_action(
        observed_expires_at=None, enrolled=False, policy_expires_at=None, **base
    ).action == ACTION_SET_EXPIRY_EXTERNAL
    assert desired_action(
        observed_expires_at=date(2027, 3, 1),
        enrolled=False,
        policy_expires_at=None,
        **base,
    ).action == ACTION_SET_EXPIRY_EXTERNAL
    assert desired_action(
        observed_expires_at=None,
        enrolled=True,
        policy_expires_at=date(2027, 2, 11),
        **base,
    ).action == ACTION_NONE
    assert desired_action(
        observed_expires_at=None,
        enrolled=True,
        policy_expires_at=date(2026, 8, 11),
        **base,
    ).action == ACTION_DISABLE


def test_internal_migration_and_reactivation_do_not_slide_each_sync():
    first = desired_action(
        classification=CLASS_INTERNAL_ACTIVE,
        is_active=True,
        observed_expires_at=None,
        today=date(2026, 8, 11),
        delete_after=None,
        enrolled=False,
        policy_expires_at=None,
        previous_active=None,
        internal_months=3,
        external_months=6,
    )
    assert first.action == ACTION_MIGRATION_CANDIDATE

    active_cycle = desired_action(
        classification=CLASS_INTERNAL_ACTIVE,
        is_active=True,
        observed_expires_at=date(2026, 11, 11),
        today=date(2026, 8, 20),
        delete_after=None,
        enrolled=True,
        policy_expires_at=date(2026, 11, 11),
        previous_active=True,
        internal_months=3,
        external_months=6,
    )
    assert active_cycle.action == ACTION_NONE

    # Истекший цикл превращается в блокировку, а не в бесконечное продление.
    overdue = desired_action(
        classification=CLASS_INTERNAL_ACTIVE,
        is_active=True,
        observed_expires_at=None,
        today=date(2026, 12, 1),
        delete_after=None,
        enrolled=True,
        policy_expires_at=date(2026, 11, 11),
        previous_active=False,
        internal_months=3,
        external_months=6,
    )
    assert overdue.action == ACTION_DISABLE

    # Новый цикл после повторного включения назначает сам lifecycle-сервис,
    # поэтому политика видит запись как еще не зачисленную.
    reactivated = desired_action(
        classification=CLASS_INTERNAL_ACTIVE,
        is_active=True,
        observed_expires_at=None,
        today=date(2026, 12, 1),
        delete_after=None,
        enrolled=False,
        policy_expires_at=None,
        previous_active=False,
        internal_months=3,
        external_months=6,
    )
    assert reactivated.action == ACTION_MIGRATION_CANDIDATE


def test_unmatched_account_is_never_disabled_automatically():
    # Нераспознанная учетка не блокируется: у действующего работника в DSM
    # может быть просто не заполнен e-mail. Отсутствие данных трактуется в
    # пользу работника, решение принимает администратор.
    for active in (True, False):
        decision = desired_action(
            classification=CLASS_UNKNOWN,
            is_active=active,
            observed_expires_at=None,
            today=date(2026, 8, 11),
            delete_after=None,
            enrolled=False,
            policy_expires_at=None,
            previous_active=None,
            internal_months=3,
            external_months=6,
        )
        assert decision.action == ACTION_CLASSIFY
        assert decision.action != ACTION_DISABLE


def test_exception_and_system_accounts_are_the_only_ones_spared():
    for classification in (CLASS_EXCEPTION, CLASS_PROTECTED):
        decision = desired_action(
            classification=classification,
            is_active=True,
            observed_expires_at=None,
            today=date(2026, 8, 11),
            delete_after=None,
            enrolled=False,
            policy_expires_at=None,
            previous_active=None,
            internal_months=3,
            external_months=6,
        )
        assert decision.action == ACTION_NONE
