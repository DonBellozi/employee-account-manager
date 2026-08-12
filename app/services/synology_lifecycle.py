from __future__ import annotations

import random
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, HRSourceRecord
from app.models_onec_sources import HREmploymentState, OneCAdditionalSource
from app.models_synology import (
    SynologyAccountState,
    SynologyControlSettings,
    SynologyException,
    SynologySyncRun,
)
from app.services.synology import SynologyLocalUser, SynologyService
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
    normalize_domain,
    normalize_email,
    normalize_login,
)


_SYNC_LOCK = threading.Lock()

CLASSIFICATION_LABELS = {
    CLASS_INTERNAL_ACTIVE: "Наш сотрудник",
    CLASS_INTERNAL_DISMISSED: "Уволен / нет среди действующих",
    CLASS_EXTERNAL: "Внешняя",
    CLASS_UNKNOWN: "Требует классификации",
    CLASS_EXCEPTION: "Исключение",
    CLASS_PROTECTED: "Защищенная DSM",
}

ACTION_LABELS = {
    ACTION_NONE: "–",
    ACTION_CLASSIFY: "Требует классификации",
    ACTION_MIGRATION_CANDIDATE: "Кандидат на 3 месяца",
    ACTION_SET_EXPIRY_INTERNAL: "Установить 3 месяца",
    ACTION_SET_EXPIRY_EXTERNAL: "Установить 6 месяцев",
    ACTION_DISABLE: "Отключить",
    ACTION_DELETE: "Удалить",
}


class SynologyLifecycleService:
    """Сверка локальных DSM-учеток с общим кадровым lifecycle проекта.

    Эта версия – первый безопасный этап: она ничего не меняет и не удаляет на
    Synology. Все вычисленные действия сохраняются как desired_action и
    показываются администратору. Это позволяет проверить классификацию,
    исключения и фактические поля DSM перед включением write-адаптера.
    """

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @property
    def local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.app_timezone))

    @property
    def today(self) -> date:
        return self.local_now.date()

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def control_settings(self) -> SynologyControlSettings:
        row = self.db.get(SynologyControlSettings, 1)
        if row is not None:
            # Первый этап никогда не должен случайно перейти в write-режим даже
            # после ручного изменения SQLite.
            if row.write_enabled:
                row.write_enabled = False
                self.db.commit()
            return row
        row = SynologyControlSettings(id=1, write_enabled=False)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def save_control_settings(
        self,
        *,
        sync_interval_minutes: int,
        migration_batch_size: int,
        migration_interval_days: int,
        internal_expiry_months: int,
        external_expiry_months: int,
        delete_after_months: int,
        actor: str,
    ) -> SynologyControlSettings:
        values = {
            "sync_interval_minutes": (sync_interval_minutes, 1, 1440),
            "migration_batch_size": (migration_batch_size, 1, 100),
            "migration_interval_days": (migration_interval_days, 1, 365),
            "internal_expiry_months": (internal_expiry_months, 1, 24),
            "external_expiry_months": (external_expiry_months, 1, 24),
            "delete_after_months": (delete_after_months, 1, 60),
        }
        for name, (value, minimum, maximum) in values.items():
            if not minimum <= int(value) <= maximum:
                raise ValueError(
                    f"{name}: допустимо от {minimum} до {maximum}"
                )

        row = self.control_settings()
        row.sync_interval_minutes = int(sync_interval_minutes)
        row.migration_batch_size = int(migration_batch_size)
        row.migration_interval_days = int(migration_interval_days)
        row.internal_expiry_months = int(internal_expiry_months)
        row.external_expiry_months = int(external_expiry_months)
        row.delete_after_months = int(delete_after_months)
        row.write_enabled = False
        row.updated_by = actor
        row.updated_at = self.utcnow()
        self.db.add(
            AuditLog(
                actor=actor,
                action="synology_policy_updated",
                target="Synology DSM",
                result="success",
                details=(
                    f"sync={row.sync_interval_minutes}m; "
                    f"batch={row.migration_batch_size}/{row.migration_interval_days}d; "
                    f"internal={row.internal_expiry_months}m; "
                    f"external={row.external_expiry_months}m; "
                    f"delete={row.delete_after_months}m; mode=observe"
                ),
            )
        )
        self.db.commit()
        return row

    def managed_domains(self) -> set[str]:
        domains = {
            normalize_domain(item)
            for item in self.settings.zimbra_domains
            if normalize_domain(item)
        }
        if normalize_domain(self.settings.onec_source_domain):
            domains.add(normalize_domain(self.settings.onec_source_domain))

        rows = self.db.scalars(
            select(OneCAdditionalSource).where(
                OneCAdditionalSource.enabled.is_(True)
            )
        ).all()
        for row in rows:
            domain = normalize_domain(row.mail_domain)
            if domain:
                domains.add(domain)
        return domains

    def _active_exceptions(self) -> tuple[dict[str, SynologyException], dict[str, SynologyException]]:
        rows = self.db.scalars(
            select(SynologyException).where(
                SynologyException.is_active.is_(True)
            )
        ).all()
        by_login = {
            normalize_login(row.login): row
            for row in rows
            if normalize_login(row.login)
        }
        by_stable = {
            row.stable_id.strip(): row
            for row in rows
            if row.stable_id.strip()
        }
        return by_login, by_stable

    def _hr_snapshot(self, email: str) -> dict[str, object]:
        normalized = normalize_email(email)
        if not normalized:
            return {
                "active": False,
                "worker_key": "",
                "match_method": "",
                "dismissal_date": None,
            }

        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    func.lower(HRSourceRecord.corporate_email) == normalized
                )
            ).all()
        )
        worker_keys = {
            record.worker_key
            for record in records
            if str(record.worker_key or "").strip()
        }
        active = any(record.is_present for record in records)

        states: list[HREmploymentState] = []
        if worker_keys:
            states = list(
                self.db.scalars(
                    select(HREmploymentState).where(
                        HREmploymentState.worker_key.in_(worker_keys)
                    )
                ).all()
            )
            if any(state.status in {"active", "scheduled"} for state in states):
                active = True

        dates = [
            state.dismissal_date
            for state in states
            if state.dismissal_date is not None
        ]
        match_method = (
            "email"
            if len(worker_keys) == 1
            else "email_ambiguous"
            if len(worker_keys) > 1
            else "managed_domain_only"
        )
        return {
            "active": active,
            "worker_key": next(iter(worker_keys)) if len(worker_keys) == 1 else "",
            "match_method": match_method,
            "dismissal_date": max(dates) if dates else None,
        }

    def _find_state(self, account: SynologyLocalUser) -> SynologyAccountState | None:
        row = self.db.scalar(
            select(SynologyAccountState).where(
                SynologyAccountState.stable_id == account.stable_id
            )
        )
        if row is not None:
            return row

        login = normalize_login(account.login)
        row = self.db.scalar(
            select(SynologyAccountState).where(
                func.lower(SynologyAccountState.login) == login
            )
        )
        if row is None:
            return None

        # Если сначала DSM не дал UID, а затем дал – тихо усиливаем стабильный
        # идентификатор, сохраняя накопленное lifecycle-состояние.
        conflict = self.db.scalar(
            select(SynologyAccountState.id).where(
                SynologyAccountState.stable_id == account.stable_id,
                SynologyAccountState.id != row.id,
            )
        )
        if conflict is None:
            row.stable_id = account.stable_id
        return row

    def _audit(
        self,
        *,
        action: str,
        target: str,
        result: str = "success",
        details: str = "",
        actor: str = "system",
    ) -> None:
        self.db.add(
            AuditLog(
                actor=actor,
                action=action,
                target=target,
                result=result,
                details=details,
            )
        )

    @staticmethod
    def _state_signature(row: SynologyAccountState) -> tuple:
        return (
            row.stable_id,
            row.login,
            row.uid,
            row.email,
            row.description,
            row.status,
            row.is_active,
            row.expires_at,
            row.is_present,
            row.classification,
            row.worker_key,
            row.match_method,
            row.dismissal_reference_date,
            row.dismissal_reference_source,
            row.delete_after,
            row.desired_action,
            row.desired_reason,
        )

    def _apply_observation(
        self,
        account: SynologyLocalUser,
        *,
        managed_domains: set[str],
        exception_by_login: dict[str, SynologyException],
        exception_by_stable: dict[str, SynologyException],
        control: SynologyControlSettings,
        now: datetime,
    ) -> tuple[SynologyAccountState, bool, bool]:
        row = self._find_state(account)
        is_new = row is None
        if row is None:
            row = SynologyAccountState(
                stable_id=account.stable_id,
                login=account.login,
                first_seen_at=now,
                last_seen_at=now,
            )
            self.db.add(row)
            previous_signature = None
            previous_classification = ""
            previous_action = ""
            previous_active = None
            previous_expiry = None
        else:
            previous_signature = self._state_signature(row)
            previous_classification = row.classification
            previous_action = row.desired_action
            previous_active = row.last_observed_active
            previous_expiry = row.last_observed_expires_at

        exception = (
            exception_by_stable.get(account.stable_id)
            or exception_by_login.get(normalize_login(account.login))
        )
        hr = self._hr_snapshot(account.email)
        classification = classify_account(
            email=account.email,
            managed_domains=managed_domains,
            protected=account.protected,
            exception=exception is not None,
            active_employee=bool(hr["active"]),
        )

        # Реальный HR worker_key нужен только при однозначном совпадении email.
        worker_key = str(hr["worker_key"] or "") if classification in {
            CLASS_INTERNAL_ACTIVE,
            CLASS_INTERNAL_DISMISSED,
        } else ""
        match_method = str(hr["match_method"] or "") if classification in {
            CLASS_INTERNAL_ACTIVE,
            CLASS_INTERNAL_DISMISSED,
        } else ""

        row.login = account.login
        row.uid = account.uid
        row.email = normalize_email(account.email)
        row.description = account.description
        row.status = account.status
        row.is_active = account.is_active
        row.expires_at = account.expires_at
        row.is_present = True
        row.last_seen_at = now
        row.classification = classification
        row.worker_key = worker_key
        row.match_method = match_method
        row.last_error = account.detail_error

        if classification == CLASS_INTERNAL_ACTIVE:
            # Новая активная занятость полностью отменяет старый countdown на
            # удаление. При следующем увольнении будет зафиксирована новая дата.
            row.dismissal_reference_date = None
            row.dismissal_reference_source = ""
            row.delete_after = None
        elif classification == CLASS_INTERNAL_DISMISSED:
            explicit_date = hr["dismissal_date"]
            if (
                explicit_date is not None
                and row.dismissal_reference_source != "hr"
            ):
                row.dismissal_reference_date = explicit_date
                row.dismissal_reference_source = "hr"
                row.delete_after = add_months(
                    explicit_date,
                    control.delete_after_months,
                )
            elif row.dismissal_reference_date is None:
                row.dismissal_reference_date = self.today
                row.dismissal_reference_source = "detected"
                row.delete_after = add_months(
                    self.today,
                    control.delete_after_months,
                )
                self._audit(
                    action="synology_dismissal_detected",
                    target=account.login,
                    details=(
                        f"reference={row.dismissal_reference_date.isoformat()}; "
                        f"source={row.dismissal_reference_source}; "
                        f"delete_after={row.delete_after.isoformat()}"
                    ),
                )
        elif classification in {CLASS_EXTERNAL, CLASS_UNKNOWN}:
            row.dismissal_reference_date = None
            row.dismissal_reference_source = ""
            row.delete_after = None

        decision = desired_action(
            classification=classification,
            is_active=account.is_active,
            observed_expires_at=account.expires_at,
            today=self.today,
            delete_after=row.delete_after,
            enrolled=row.cycle_started_at is not None,
            policy_expires_at=row.policy_expires_at,
            previous_active=previous_active,
            internal_months=control.internal_expiry_months,
            external_months=control.external_expiry_months,
        )
        row.desired_action = decision.action
        row.desired_reason = decision.reason

        if is_new:
            self._audit(
                action="synology_account_discovered",
                target=account.login,
                details=(
                    f"classification={classification}; "
                    f"email={row.email or '-'}; status={account.status}"
                ),
            )
        elif previous_classification != classification:
            self._audit(
                action="synology_classification_changed",
                target=account.login,
                details=f"{previous_classification or '-'} -> {classification}",
            )

        if previous_active is False and account.is_active:
            self._audit(
                action=(
                    "synology_dismissed_reactivated"
                    if classification == CLASS_INTERNAL_DISMISSED
                    else "synology_account_reactivated"
                ),
                target=account.login,
                result="warning",
                details=f"classification={classification}",
            )

        if (
            previous_expiry is not None
            and account.expires_at is None
            and classification not in {CLASS_EXCEPTION, CLASS_PROTECTED, CLASS_UNKNOWN}
        ):
            self._audit(
                action="synology_expiration_removed",
                target=account.login,
                result="warning",
                details=f"classification={classification}",
            )

        if (
            previous_action != row.desired_action
            and row.desired_action not in {ACTION_NONE, ACTION_MIGRATION_CANDIDATE}
        ):
            self._audit(
                action="synology_action_required",
                target=account.login,
                result="warning",
                details=(
                    f"action={row.desired_action}; "
                    f"reason={row.desired_reason}; mode=observe"
                ),
            )

        row.last_observed_active = account.is_active
        row.last_observed_expires_at = account.expires_at

        changed = (
            previous_signature is None
            or previous_signature != self._state_signature(row)
        )
        return row, is_new, changed

    def _stage_migration_batch(
        self,
        *,
        control: SynologyControlSettings,
        now: datetime,
    ) -> int:
        """Выбрать следующий ограниченный пакет внутренних локальных учеток.

        В read-only этапе это только фиксация плана в нашей БД. Следующий пакет
        не выбирается, пока предыдущий все еще ожидает фактической установки
        срока. Поэтому наблюдательный этап не накопит десятки неисполненных
        пакетов и не превратит постепенную миграцию в массовую.
        """
        pending = int(
            self.db.scalar(
                select(func.count(SynologyAccountState.id)).where(
                    SynologyAccountState.is_present.is_(True),
                    SynologyAccountState.classification == CLASS_INTERNAL_ACTIVE,
                    SynologyAccountState.desired_action == ACTION_SET_EXPIRY_INTERNAL,
                )
            )
            or 0
        )
        if pending:
            return 0

        last_batch = control.last_migration_batch_at
        if last_batch is not None:
            if last_batch.tzinfo is None:
                last_batch = last_batch.replace(tzinfo=timezone.utc)
            due_at = last_batch.astimezone(timezone.utc) + timedelta(
                days=max(1, int(control.migration_interval_days))
            )
            if now.astimezone(timezone.utc) < due_at:
                return 0

        candidates = list(
            self.db.scalars(
                select(SynologyAccountState).where(
                    SynologyAccountState.is_present.is_(True),
                    SynologyAccountState.classification == CLASS_INTERNAL_ACTIVE,
                    SynologyAccountState.is_active.is_(True),
                    SynologyAccountState.cycle_started_at.is_(None),
                    SynologyAccountState.desired_action == ACTION_MIGRATION_CANDIDATE,
                )
            ).all()
        )
        if not candidates:
            return 0

        count = min(max(1, int(control.migration_batch_size)), len(candidates))
        selected = random.SystemRandom().sample(candidates, count)
        policy_expiry = add_months(self.today, control.internal_expiry_months)
        for row in selected:
            if row.lifecycle_started_at is None:
                row.lifecycle_started_at = now
            row.cycle_started_at = now
            row.policy_expires_at = policy_expiry
            row.desired_action = ACTION_SET_EXPIRY_INTERNAL
            row.desired_reason = (
                "Выбран в очередной пакет постепенной миграции; "
                f"плановый срок {policy_expiry.isoformat()}."
            )
            self._audit(
                action="synology_migration_selected",
                target=row.login,
                details=(
                    f"policy_expires_at={policy_expiry.isoformat()}; "
                    f"batch_size={count}; mode=observe"
                ),
            )

        control.last_migration_batch_at = now
        return count

    def sync(self, *, trigger: str = "manual") -> SynologySyncRun:
        if not self.settings.synology_enabled:
            raise RuntimeError("Интеграция Synology отключена: SYNOLOGY_ENABLED=false")
        if not _SYNC_LOCK.acquire(blocking=False):
            raise RuntimeError("Синхронизация Synology уже выполняется")

        run = SynologySyncRun(trigger=trigger, status="running")
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        try:
            accounts = SynologyService(self.settings).list_accounts()
            control = self.control_settings()
            domains = self.managed_domains()
            by_login, by_stable = self._active_exceptions()
            now = self.utcnow()

            states = list(self.db.scalars(select(SynologyAccountState)).all())
            previously_present = {
                row.id: row.is_present
                for row in states
                if row.id is not None
            }
            for row in states:
                row.is_present = False

            new_count = 0
            changed_count = 0
            planned = 0
            detail_errors = 0
            seen_ids: set[int] = set()

            for account in accounts:
                row, is_new, changed = self._apply_observation(
                    account,
                    managed_domains=domains,
                    exception_by_login=by_login,
                    exception_by_stable=by_stable,
                    control=control,
                    now=now,
                )
                self.db.flush()
                if row.id is not None:
                    seen_ids.add(row.id)
                new_count += int(is_new)
                changed_count += int(changed)
                detail_errors += int(bool(account.detail_error))
                if row.desired_action != ACTION_NONE:
                    planned += 1

            # Выбираем только один ограниченный пакет внутренних учеток. Пока
            # выбранный пакет не исполнен, следующий не формируется.
            self._stage_migration_batch(control=control, now=now)

            # Пропавшая учетная запись остается в истории, но больше не
            # участвует в lifecycle. Отдельный AuditLog на каждый обычный sync
            # не создаем, чтобы журнал не превращался в технический шум.
            for row in states:
                if row.id in seen_ids:
                    continue
                if previously_present.get(row.id, False):
                    row.desired_action = ACTION_NONE
                    row.desired_reason = "Учетная запись отсутствует в текущем списке DSM."

            run.users_count = len(accounts)
            run.new_accounts = new_count
            run.changed_accounts = changed_count
            run.planned_actions = planned
            run.detail_errors = detail_errors
            run.status = "partial" if detail_errors else "success"
            run.message = (
                f"users={len(accounts)}; new={new_count}; changed={changed_count}; "
                f"planned={planned}; detail_errors={detail_errors}; mode=observe"
            )
            run.completed_at = self.utcnow()
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            failed = self.db.get(SynologySyncRun, run.id)
            if failed is not None:
                failed.status = "failed"
                failed.error_message = str(exc)
                failed.completed_at = self.utcnow()
                self._audit(
                    action="synology_sync_failed",
                    target=self.settings.synology_ssh_host or "Synology DSM",
                    result="error",
                    details=str(exc),
                )
                self.db.commit()
                return failed
            raise
        finally:
            _SYNC_LOCK.release()

    def add_exception(
        self,
        *,
        login: str,
        stable_id: str = "",
        reason: str,
        actor: str,
    ) -> SynologyException:
        normalized = normalize_login(login)
        if not normalized:
            raise ValueError("Укажите login Synology")
        clean_reason = " ".join(str(reason or "").split())
        if not clean_reason:
            raise ValueError("Укажите причину исключения")

        row = self.db.scalar(
            select(SynologyException).where(
                func.lower(SynologyException.login) == normalized
            )
        )
        if row is None:
            row = SynologyException(
                login=normalized,
                stable_id=str(stable_id or "").strip(),
                reason=clean_reason,
                is_active=True,
                created_by=actor,
                created_at=self.utcnow(),
            )
            self.db.add(row)
        else:
            row.stable_id = str(stable_id or row.stable_id or "").strip()
            row.reason = clean_reason
            row.is_active = True
            row.removed_by = ""
            row.removed_at = None

        states = list(
            self.db.scalars(
                select(SynologyAccountState).where(
                    or_(
                        func.lower(SynologyAccountState.login) == normalized,
                        SynologyAccountState.stable_id == row.stable_id,
                    )
                )
            ).all()
        )
        for state in states:
            state.classification = CLASS_EXCEPTION
            state.desired_action = ACTION_NONE
            state.desired_reason = "Учетная запись находится в списке исключений."

        self._audit(
            action="synology_exception_added",
            target=normalized,
            details=clean_reason,
            actor=actor,
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def remove_exception(self, exception_id: int, *, actor: str) -> SynologyException:
        row = self.db.get(SynologyException, int(exception_id))
        if row is None:
            raise ValueError("Исключение не найдено")
        if row.is_active:
            row.is_active = False
            row.removed_by = actor
            row.removed_at = self.utcnow()
            self._audit(
                action="synology_exception_removed",
                target=row.login,
                details=row.reason,
                actor=actor,
            )
            self.db.commit()
        return row

    def connection_view(self) -> dict[str, object]:
        auth = self.settings.synology_ssh_auth
        password_present = bool(
            self.settings.synology_ssh_password
            or self.settings.synology_ssh_password_file
        )
        key_present = bool(
            self.settings.synology_ssh_private_key
            and Path(self.settings.synology_ssh_private_key).is_file()
        )
        auth_ready = (
            key_present
            if auth == "key"
            else password_present
            if auth == "password"
            else key_present or password_present
        )
        configured = bool(
            self.settings.synology_ssh_host
            and self.settings.synology_ssh_user
            and auth_ready
        )
        known_hosts = Path(self.settings.synology_ssh_known_hosts)
        return {
            "enabled": self.settings.synology_enabled,
            "configured": configured,
            "host": self.settings.synology_ssh_host or "–",
            "port": self.settings.synology_ssh_port,
            "user": self.settings.synology_ssh_user or "–",
            "auth": auth,
            "known_hosts": "найден" if known_hosts.is_file() else "не найден",
            "sudo": "Да" if self.settings.synology_ssh_use_sudo else "Нет",
            "command": self.settings.synology_synouser_command,
            "mode": "Только чтение",
        }

    def view(self, *, limit: int = 2000) -> dict[str, object]:
        control = self.control_settings()
        latest = self.db.scalar(
            select(SynologySyncRun)
            .order_by(SynologySyncRun.id.desc())
            .limit(1)
        )
        runs = list(
            self.db.scalars(
                select(SynologySyncRun)
                .order_by(SynologySyncRun.id.desc())
                .limit(10)
            ).all()
        )
        states = list(
            self.db.scalars(
                select(SynologyAccountState)
                .where(SynologyAccountState.is_present.is_(True))
                .order_by(SynologyAccountState.login)
                .limit(max(1, min(int(limit), 10000)))
            ).all()
        )
        exceptions = list(
            self.db.scalars(
                select(SynologyException)
                .where(SynologyException.is_active.is_(True))
                .order_by(SynologyException.login)
            ).all()
        )
        counts = Counter(row.classification for row in states)

        account_rows: list[dict[str, object]] = []
        for row in states:
            proposed_expiry = None
            if row.desired_action == ACTION_SET_EXPIRY_EXTERNAL:
                proposed_expiry = add_months(
                    self.today,
                    control.external_expiry_months,
                )
            elif row.desired_action == ACTION_SET_EXPIRY_INTERNAL:
                proposed_expiry = add_months(
                    self.today,
                    control.internal_expiry_months,
                )
            account_rows.append(
                {
                    "row": row,
                    "classification_label": CLASSIFICATION_LABELS.get(
                        row.classification, row.classification
                    ),
                    "action_label": ACTION_LABELS.get(
                        row.desired_action, row.desired_action
                    ),
                    "proposed_expiry": proposed_expiry,
                    "linked": bool(row.worker_key),
                }
            )

        return {
            "connection": self.connection_view(),
            "control": control,
            "latest": latest,
            "runs": runs,
            "accounts": account_rows,
            "exceptions": exceptions,
            "managed_domains": sorted(self.managed_domains()),
            "counts": {
                "total": len(states),
                "internal_active": counts[CLASS_INTERNAL_ACTIVE],
                "internal_dismissed": counts[CLASS_INTERNAL_DISMISSED],
                "external": counts[CLASS_EXTERNAL],
                "unknown": counts[CLASS_UNKNOWN],
                "exceptions": counts[CLASS_EXCEPTION],
                "protected": counts[CLASS_PROTECTED],
                "actions": sum(
                    1 for row in states if row.desired_action != ACTION_NONE
                ),
            },
            "observe_only": True,
        }
