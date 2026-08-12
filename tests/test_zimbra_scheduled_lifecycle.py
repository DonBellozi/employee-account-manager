from __future__ import annotations

from types import SimpleNamespace

from app.models_zimbra_observer import ZimbraLifecycleState
from app.services.zimbra_scheduled_lifecycle import ZimbraScheduledLifecycleExecutor


def test_close_permission_enables_only_close():
    config = SimpleNamespace(
        allow_close=True,
        allow_backup=False,
        allow_delete=False,
    )
    assert ZimbraScheduledLifecycleExecutor._allowed_recommendations(config) == {"close"}


def test_backup_without_delete_is_not_automatic():
    config = SimpleNamespace(
        allow_close=False,
        allow_backup=True,
        allow_delete=False,
    )
    assert ZimbraScheduledLifecycleExecutor._allowed_recommendations(config) == set()


def test_backup_delete_requires_both_permissions():
    config = SimpleNamespace(
        allow_close=False,
        allow_backup=True,
        allow_delete=True,
    )
    assert ZimbraScheduledLifecycleExecutor._allowed_recommendations(config) == {
        "archive_delete"
    }


def test_close_and_backup_delete_can_run_in_same_cycle():
    config = SimpleNamespace(
        allow_close=True,
        allow_backup=True,
        allow_delete=True,
    )
    assert ZimbraScheduledLifecycleExecutor._allowed_recommendations(config) == {
        "close",
        "archive_delete",
    }


def test_state_addresses_include_primary_and_aliases():
    state = ZimbraLifecycleState(
        account_key="z1",
        primary_email="User@Domain.RU",
        addresses_json='["alias@domain.com", "USER@DOMAIN.RU"]',
    )
    assert ZimbraScheduledLifecycleExecutor._state_addresses(state) == {
        "user@domain.ru",
        "alias@domain.com",
    }


def test_broken_alias_json_fails_closed_to_primary_only():
    state = ZimbraLifecycleState(
        account_key="z1",
        primary_email="user@domain.ru",
        addresses_json="not-json",
    )
    assert ZimbraScheduledLifecycleExecutor._state_addresses(state) == {
        "user@domain.ru"
    }
