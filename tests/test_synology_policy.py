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
    # Ключевое правило: корпоративный домен + отсутствует среди действующих
    # работников всех организаций = уволенная локальная учетка, даже если
    # worker_key не найден.
    assert classify_account(
        email="former@corp.ru",
        managed_domains=domains,
        protected=False,
        exception=False,
        active_employee=False,
    ) == CLASS_INTERNAL_DISMISSED


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


def test_dismissed_account_disable_then_delete():
    decision = desired_action(
        classification=CLASS_INTERNAL_DISMISSED,
        is_active=True,
        observed_expires_at=None,
        today=date(2026, 8, 11),
        delete_after=date(2027, 2, 11),
        enrolled=False,
        policy_expires_at=None,
        previous_active=None,
        internal_months=3,
        external_months=6,
    )
    assert decision.action == ACTION_DISABLE

    decision = desired_action(
        classification=CLASS_INTERNAL_DISMISSED,
        is_active=False,
        observed_expires_at=None,
        today=date(2027, 2, 11),
        delete_after=date(2027, 2, 11),
        enrolled=True,
        policy_expires_at=None,
        previous_active=False,
        internal_months=3,
        external_months=6,
    )
    assert decision.action == ACTION_DELETE


def test_external_never_becomes_unlimited():
    base = dict(
        classification=CLASS_EXTERNAL,
        is_active=True,
        today=date(2026, 8, 11),
        delete_after=None,
        enrolled=False,
        policy_expires_at=None,
        previous_active=True,
        internal_months=3,
        external_months=6,
    )
    assert desired_action(observed_expires_at=None, **base).action == ACTION_SET_EXPIRY_EXTERNAL
    assert desired_action(observed_expires_at=date(2027, 3, 1), **base).action == ACTION_SET_EXPIRY_EXTERNAL
    assert desired_action(observed_expires_at=date(2027, 2, 11), **base).action == ACTION_NONE


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

    reactivated = desired_action(
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
    assert reactivated.action == ACTION_SET_EXPIRY_INTERNAL


def test_unknown_requires_manual_classification():
    decision = desired_action(
        classification=CLASS_UNKNOWN,
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
    assert decision.action == ACTION_CLASSIFY
