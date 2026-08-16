from __future__ import annotations

from datetime import date

from app.services.synology_policy import (
    ACTION_DISABLE,
    ACTION_MIGRATION_CANDIDATE,
    ACTION_NONE,
    ACTION_SET_EXPIRY_EXTERNAL,
    CLASS_EXCEPTION,
    CLASS_EXTERNAL,
    CLASS_INTERNAL_ACTIVE,
    CLASS_INTERNAL_DISMISSED,
    CLASS_PROTECTED,
    CLASS_UNKNOWN,
    classify_account,
    desired_action,
)


DOMAINS = {"domain1.ru", "domain2.ru"}
TODAY = date(2026, 8, 16)


def decide(classification: str, *, enrolled: bool = False, due: date | None = None):
    return desired_action(
        classification=classification,
        is_active=True,
        observed_expires_at=None,
        today=TODAY,
        delete_after=None,
        enrolled=enrolled,
        policy_expires_at=due,
        previous_active=True,
        internal_months=3,
        external_months=6,
    ).action


def test_exception_has_priority_over_active_employee():
    assert classify_account(
        email="ivanov@domain1.ru",
        managed_domains=DOMAINS,
        protected=False,
        exception=True,
        active_employee=True,
        matched_employee=True,
    ) == CLASS_EXCEPTION


def test_protected_system_has_highest_priority():
    assert classify_account(
        email="root@domain1.ru",
        managed_domains=DOMAINS,
        protected=True,
        exception=True,
        active_employee=True,
    ) == CLASS_PROTECTED


def test_internal_email_present_in_its_org_gets_three_month_cycle():
    classification = classify_account(
        email="ivanov@domain1.ru",
        managed_domains=DOMAINS,
        protected=False,
        exception=False,
        active_employee=True,
        matched_employee=True,
    )
    assert classification == CLASS_INTERNAL_ACTIVE
    assert decide(classification) == ACTION_MIGRATION_CANDIDATE


def test_internal_email_missing_from_active_hr_is_blocked_immediately():
    classification = classify_account(
        email="ivanov@domain2.ru",
        managed_domains=DOMAINS,
        protected=False,
        exception=False,
        active_employee=False,
        matched_employee=False,
    )
    assert classification == CLASS_INTERNAL_DISMISSED
    assert decide(classification) == ACTION_DISABLE


def test_external_domain_stays_external_even_if_person_is_known_active():
    classification = classify_account(
        email="ivanov@gmail.test",
        managed_domains=DOMAINS,
        protected=False,
        exception=False,
        active_employee=True,
        matched_employee=True,
    )
    assert classification == CLASS_EXTERNAL
    assert decide(classification) == ACTION_SET_EXPIRY_EXTERNAL


def test_empty_email_is_manual_classification_not_auto_block():
    classification = classify_account(
        email="",
        managed_domains=DOMAINS,
        protected=False,
        exception=False,
        active_employee=False,
    )
    assert classification == CLASS_UNKNOWN
    assert decide(classification) != ACTION_DISABLE


def test_due_internal_cycle_blocks_but_never_deletes():
    action = decide(CLASS_INTERNAL_ACTIVE, enrolled=True, due=date(2026, 8, 1))
    assert action == ACTION_DISABLE


def test_already_disabled_internal_dismissed_is_left_disabled():
    action = desired_action(
        classification=CLASS_INTERNAL_DISMISSED,
        is_active=False,
        observed_expires_at=None,
        today=TODAY,
        delete_after=None,
        enrolled=False,
        policy_expires_at=None,
        previous_active=False,
        internal_months=3,
        external_months=6,
    ).action
    assert action == ACTION_NONE
