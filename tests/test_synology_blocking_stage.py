from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.synology import SynologyLocalUser, SynologyService
from app.services.synology_policy import (
    ACTION_CLASSIFY,
    ACTION_DISABLE,
    ACTION_MIGRATION_CANDIDATE,
    ACTION_NONE,
    CLASS_EXTERNAL,
    CLASS_INTERNAL_ACTIVE,
    CLASS_INTERNAL_DISMISSED,
    CLASS_UNKNOWN,
    desired_action,
)


def _decision(
    classification: str,
    *,
    active: bool = True,
    enrolled: bool = True,
    expires: date | None = date(2026, 11, 15),
):
    return desired_action(
        classification=classification,
        is_active=active,
        observed_expires_at=None,
        today=date(2026, 8, 15),
        delete_after=date(2020, 1, 1),
        enrolled=enrolled,
        policy_expires_at=expires,
        previous_active=None,
        internal_months=3,
        external_months=6,
    )


def test_enum_footer_is_not_a_login():
    output = "baranov.gb\npartner.ext\n294 User Listed:\n"
    assert SynologyService._parse_enum_output(output) == ["baranov.gb", "partner.ext"]


def test_enum_footer_plural_is_not_a_login():
    output = "baranov.gb\n294 Users Listed:\n"
    assert SynologyService._parse_enum_output(output) == ["baranov.gb"]


def test_internal_missing_from_hr_is_disabled_not_deleted():
    decision = _decision(CLASS_INTERNAL_DISMISSED, active=True)
    assert decision.action == ACTION_DISABLE


def test_internal_active_waits_for_three_month_cycle():
    decision = _decision(CLASS_INTERNAL_ACTIVE, active=True, enrolled=False, expires=None)
    assert decision.action == ACTION_MIGRATION_CANDIDATE


def test_internal_active_disabled_is_never_reenabled_or_enrolled():
    decision = _decision(CLASS_INTERNAL_ACTIVE, active=False, enrolled=False, expires=None)
    assert decision.action == ACTION_NONE


def test_internal_active_blocks_when_cycle_is_due():
    decision = _decision(
        CLASS_INTERNAL_ACTIVE,
        active=True,
        enrolled=True,
        expires=date(2026, 8, 15),
    )
    assert decision.action == ACTION_DISABLE


def test_external_blocks_when_six_month_cycle_is_due():
    decision = _decision(
        CLASS_EXTERNAL,
        active=True,
        enrolled=True,
        expires=date(2026, 8, 14),
    )
    assert decision.action == ACTION_DISABLE


def test_unknown_account_is_disabled():
    decision = _decision(CLASS_UNKNOWN, active=True, enrolled=False, expires=None)
    # Нераспознанная учетка (нет пригодного e-mail) больше не остается
    # висеть в статусе «требует классификации»: под ней неизвестно кто
    # заходит, поэтому она отключается.
    assert decision.action == ACTION_DISABLE
    assert decision.action != ACTION_CLASSIFY


def test_expire_account_uses_verified_modify_signature(monkeypatch):
    settings = SimpleNamespace()
    service = SynologyService(settings)

    class FakeClient:
        def close(self):
            pass

    client = FakeClient()
    monkeypatch.setattr(service, "_client", lambda: client)

    before = """
User Name: [baranov.gb]
User uid: [1531]
Fullname: [Баранов Герман Борисович]
Expired: [false]
User Mail: [baranov.gb@domain.ru]
Member Of: [100] users
""".strip()
    after = before.replace("Expired: [false]", "Expired: [true]")

    calls: list[list[str]] = []
    get_count = 0

    def fake_execute(_client, args, *, allow_nonzero=False):
        nonlocal get_count
        calls.append(list(args))
        if args[:2] == ["--get", "baranov.gb"]:
            get_count += 1
            return before if get_count == 1 else after
        if args[0] == "--modify":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(service, "_execute", fake_execute)

    account = SynologyLocalUser(
        login="baranov.gb",
        stable_id="uid:1531",
        uid="1531",
        email="baranov.gb@domain.ru",
        description="Баранов Герман Борисович",
        status="active",
        is_active=True,
    )
    result = service.expire_account(account)

    assert result.is_active is False
    assert [
        "--modify",
        "baranov.gb",
        "Баранов Герман Борисович",
        "1",
        "baranov.gb@domain.ru",
    ] in calls


def test_expire_account_blocks_entry_without_email(monkeypatch):
    """Учетка без e-mail отключается: неизвестно, кто под ней заходит."""
    service = SynologyService(SimpleNamespace())

    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(service, "_client", lambda: FakeClient())

    before = """
User Name: [legacy.share]
User uid: [1601]
Fullname: []
Expired: [false]
User Mail: []
""".strip()
    after = before.replace("Expired: [false]", "Expired: [true]")

    calls: list[list[str]] = []
    get_count = 0

    def fake_execute(_client, args, *, allow_nonzero=False):
        nonlocal get_count
        calls.append(list(args))
        if args[:2] == ["--get", "legacy.share"]:
            get_count += 1
            return before if get_count == 1 else after
        if args[0] == "--modify":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(service, "_execute", fake_execute)

    account = SynologyLocalUser(
        login="legacy.share",
        stable_id="uid:1601",
        uid="1601",
        email="",
        status="active",
        is_active=True,
    )
    result = service.expire_account(account)

    assert result.is_active is False
    # В DSM передается ровно то, что там уже было: пустой e-mail остается пустым.
    assert ["--modify", "legacy.share", "", "1", ""] in calls


def test_expire_account_still_refuses_system_entries(monkeypatch):
    service = SynologyService(SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_client",
        lambda: (_ for _ in ()).throw(AssertionError("SSH не нужен")),
    )

    account = SynologyLocalUser(
        login="admin",
        stable_id="uid:1024",
        uid="1024",
        email="admin@domain.ru",
        status="active",
        is_active=True,
        protected=True,
    )
    try:
        service.expire_account(account)
    except RuntimeError as exc:
        assert "защищенная" in str(exc)
    else:
        raise AssertionError("защищенная учетка не должна изменяться")
