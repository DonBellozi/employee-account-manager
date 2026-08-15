from __future__ import annotations

import itertools
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import AuditLog
from app.models_synology import (
    SynologyAccountState,
    SynologyControlSettings,
)
from app.services.synology import SynologyLocalUser, SynologyService
from app.services.synology_lifecycle import (
    MASS_DISABLE_ACK_TTL_MINUTES,
    SynologyLifecycleService,
)
from app.services.synology_policy import ACTION_DISABLE, ACTION_NONE, CLASS_EXTERNAL


class _FakeDSM:
    """Заглушка DSM: считает вызовы и может падать на конкретных логинах."""

    def __init__(self, settings, *, fail_on: set[str] | None = None):
        self.settings = settings
        self.fail_on = fail_on or set()
        _FakeDSM.calls = getattr(_FakeDSM, "calls", [])

    def expire_account(self, account: SynologyLocalUser) -> SynologyLocalUser:
        _FakeDSM.calls.append(account.login)
        if account.login in self.fail_on:
            raise RuntimeError(f"DSM недоступен для {account.login}")
        return SynologyLocalUser(
            login=account.login,
            stable_id=account.stable_id,
            uid=account.uid,
            email=account.email,
            status="expired",
            is_active=False,
        )


class SynologyMassDisableGuardTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = Settings(
            app_secret_key="test-secret-key-1234567890",
            synology_enabled=True,
            synology_ssh_host="nas.test",
            zimbra_domains=["corp.test"],
        )
        _FakeDSM.calls = []

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self) -> SynologyLifecycleService:
        return SynologyLifecycleService(self.settings, self.db)

    def control(self, *, limit: int) -> SynologyControlSettings:
        row = self.service().control_settings()
        row.write_enabled = True
        row.max_disables_per_run = limit
        self.db.commit()
        return row

    def add_candidates(self, count: int, *, start: int = 0) -> list[SynologyLocalUser]:
        """Внешние учетки с истекшим циклом контроля: их ждет отключение."""
        expired = date.today() - timedelta(days=30)
        accounts: list[SynologyLocalUser] = []
        for index in range(start, start + count):
            login = f"partner{index}"
            stable_id = f"uid:{2000 + index}"
            email = f"{login}@external.test"
            self.db.add(
                SynologyAccountState(
                    stable_id=stable_id,
                    login=login,
                    uid=str(2000 + index),
                    email=email,
                    status="active",
                    is_active=True,
                    is_present=True,
                    classification=CLASS_EXTERNAL,
                    cycle_started_at=datetime.now(timezone.utc) - timedelta(days=200),
                    policy_expires_at=expired,
                    desired_action=ACTION_DISABLE,
                    desired_reason="Истек срок контроля.",
                    last_observed_active=True,
                )
            )
            accounts.append(
                SynologyLocalUser(
                    login=login,
                    stable_id=stable_id,
                    uid=str(2000 + index),
                    email=email,
                    status="active",
                    is_active=True,
                )
            )
        self.db.commit()
        return accounts

    def run_disables(self, accounts, control):
        service = self.service()
        return service._execute_disables(
            accounts=accounts,
            control=control,
            managed_domains={"corp.test"},
            exception_by_login={},
            exception_by_stable={},
        )

    def audit_actions(self) -> list[str]:
        return list(self.db.scalars(select(AuditLog.action)).all())

    # --- предохранитель -------------------------------------------------

    def test_guard_blocks_entire_stage_when_candidates_exceed_limit(self):
        control = self.control(limit=3)
        accounts = self.add_candidates(5)

        with patch(
            "app.services.synology_lifecycle.SynologyService",
            _FakeDSM,
        ):
            success, failed, deferred, guard = self.run_disables(accounts, control)

        self.assertEqual(success, 0)
        self.assertEqual(failed, 0)
        self.assertEqual(deferred, 5)
        self.assertIn("предохранитель", guard.lower())
        # Ни одного обращения к DSM: частичное исполнение здесь недопустимо.
        self.assertEqual(_FakeDSM.calls, [])
        self.assertIn("synology_mass_disable_blocked", self.audit_actions())

    def test_disables_run_normally_within_limit(self):
        control = self.control(limit=10)
        accounts = self.add_candidates(4)

        with patch(
            "app.services.synology_lifecycle.SynologyService",
            _FakeDSM,
        ):
            success, failed, deferred, guard = self.run_disables(accounts, control)

        self.assertEqual(success, 4)
        self.assertEqual(failed, 0)
        self.assertEqual(guard, "")
        self.assertEqual(len(_FakeDSM.calls), 4)

    def test_acknowledgement_releases_guard_once(self):
        control = self.control(limit=2)
        accounts = self.add_candidates(5)
        self.service().acknowledge_mass_disable(actor="admin")
        self.db.refresh(control)

        with patch(
            "app.services.synology_lifecycle.SynologyService",
            _FakeDSM,
        ):
            success, _failed, _deferred, guard = self.run_disables(accounts, control)

        self.assertEqual(success, 5)
        self.assertEqual(guard, "")
        self.assertIn("synology_mass_disable_confirmed", self.audit_actions())
        # Подтверждение одноразовое.
        self.db.refresh(control)
        self.assertIsNone(control.mass_disable_ack_at)
        self.assertEqual(control.mass_disable_ack_count, 0)

    def test_acknowledgement_does_not_cover_a_larger_batch(self):
        control = self.control(limit=2)
        accounts = self.add_candidates(4)
        self.service().acknowledge_mass_disable(actor="admin")
        self.db.refresh(control)
        self.assertEqual(control.mass_disable_ack_count, 4)

        # После подтверждения выборка выросла: это уже другая ситуация.
        accounts += self.add_candidates(4, start=4)

        with patch(
            "app.services.synology_lifecycle.SynologyService",
            _FakeDSM,
        ):
            success, _failed, _deferred, guard = self.run_disables(accounts, control)

        self.assertEqual(success, 0)
        self.assertNotEqual(guard, "")
        self.assertEqual(_FakeDSM.calls, [])

    def test_acknowledgement_expires(self):
        control = self.control(limit=2)
        accounts = self.add_candidates(5)
        self.service().acknowledge_mass_disable(actor="admin")
        self.db.refresh(control)
        control.mass_disable_ack_at = datetime.now(timezone.utc) - timedelta(
            minutes=MASS_DISABLE_ACK_TTL_MINUTES + 1
        )
        self.db.commit()

        with patch(
            "app.services.synology_lifecycle.SynologyService",
            _FakeDSM,
        ):
            success, _failed, _deferred, guard = self.run_disables(accounts, control)

        self.assertEqual(success, 0)
        self.assertNotEqual(guard, "")

    def test_acknowledgement_is_rejected_when_guard_did_not_trigger(self):
        self.control(limit=10)
        self.add_candidates(3)
        with self.assertRaises(ValueError):
            self.service().acknowledge_mass_disable(actor="admin")

    def test_changing_limit_drops_previous_acknowledgement(self):
        control = self.control(limit=2)
        self.add_candidates(5)
        self.service().acknowledge_mass_disable(actor="admin")
        self.db.refresh(control)
        self.assertIsNotNone(control.mass_disable_ack_at)

        self.service().save_control_settings(
            sync_interval_minutes=15,
            migration_batch_size=5,
            migration_interval_days=7,
            internal_expiry_months=3,
            external_expiry_months=6,
            delete_after_months=6,
            max_disables_per_run=50,
            write_enabled=True,
            actor="admin",
        )
        self.db.refresh(control)
        self.assertIsNone(control.mass_disable_ack_at)

    def test_limit_is_validated(self):
        with self.assertRaises(ValueError):
            self.service().save_control_settings(
                sync_interval_minutes=15,
                migration_batch_size=5,
                migration_interval_days=7,
                internal_expiry_months=3,
                external_expiry_months=6,
                delete_after_months=6,
                max_disables_per_run=0,
                write_enabled=False,
                actor="admin",
            )

    # --- устойчивость журнала -------------------------------------------

    def test_applied_disable_survives_a_later_failure(self):
        """Ошибка на второй учетке не должна стирать журнал по первой."""
        control = self.control(limit=10)
        accounts = self.add_candidates(2)

        def factory(settings):
            return _FakeDSM(settings, fail_on={"partner1"})

        with patch(
            "app.services.synology_lifecycle.SynologyService",
            factory,
        ):
            success, failed, _deferred, guard = self.run_disables(accounts, control)

        self.assertEqual(success, 1)
        self.assertEqual(failed, 1)
        self.assertEqual(guard, "")

        # Откатываем сессию так же, как это делает обработчик ошибок sync().
        self.db.rollback()
        first = self.db.scalar(
            select(SynologyAccountState).where(
                SynologyAccountState.login == "partner0"
            )
        )
        self.assertFalse(first.is_active)
        self.assertEqual(first.last_action, "disable")
        self.assertEqual(first.desired_action, ACTION_NONE)
        self.assertIn("synology_account_disabled", self.audit_actions())
        self.assertIn("synology_disable_failed", self.audit_actions())


class _HangingChannel:
    def __init__(self):
        self.closed = False

    def settimeout(self, value):
        return None

    def shutdown_write(self):
        return None

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return False

    def recv_exit_status(self):
        raise AssertionError("recv_exit_status не должен вызываться при зависании")

    def close(self):
        self.closed = True


class _ScriptedChannel(_HangingChannel):
    def __init__(self, out: bytes = b"", err: bytes = b"", code: int = 0):
        super().__init__()
        self._out = out
        self._err = err
        self._code = code

    def recv_ready(self):
        return bool(self._out)

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv(self, size):
        chunk, self._out = self._out[:size], self._out[size:]
        return chunk

    def recv_stderr(self, size):
        chunk, self._err = self._err[:size], self._err[size:]
        return chunk

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self._code


class _FakeStream:
    def __init__(self, channel):
        self.channel = channel


class _FakeClient:
    def __init__(self, channel):
        self._channel = channel

    def exec_command(self, command, timeout=None):
        stream = _FakeStream(self._channel)
        return stream, stream, stream


class SynologyCommandTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            app_secret_key="test-secret-key-1234567890",
            synology_command_timeout_seconds=5,
        )
        self.service = SynologyService(self.settings)

    def test_hanging_command_raises_instead_of_blocking_forever(self):
        channel = _HangingChannel()
        client = _FakeClient(channel)
        # Виртуальные часы: тест не должен ждать реальный таймаут.
        clock = itertools.count(0, 10)
        with patch("app.services.synology.time.monotonic", side_effect=clock):
            with self.assertRaises(TimeoutError):
                self.service._execute(client, ["--enum", "local"])
        self.assertTrue(channel.closed)

    def test_output_is_collected_and_channel_is_closed(self):
        channel = _ScriptedChannel(out=b"login-a\nlogin-b\n")
        client = _FakeClient(channel)
        result = self.service._execute(client, ["--enum", "local"])
        self.assertEqual(result, "login-a\nlogin-b")
        self.assertTrue(channel.closed)

    def test_nonzero_exit_raises_with_output(self):
        channel = _ScriptedChannel(err=b"no such user", code=255)
        client = _FakeClient(channel)
        with self.assertRaises(RuntimeError) as ctx:
            self.service._execute(client, ["--get", "ghost"])
        self.assertIn("255", str(ctx.exception))
        self.assertIn("no such user", str(ctx.exception))

    def test_nonzero_exit_can_be_allowed(self):
        channel = _ScriptedChannel(out=b"usage: synouser", code=1)
        client = _FakeClient(channel)
        result = self.service._execute(client, ["--help"], allow_nonzero=True)
        self.assertEqual(result, "usage: synouser")


if __name__ == "__main__":
    unittest.main()
