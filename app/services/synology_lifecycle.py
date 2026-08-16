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
from app.models import (
    AuditLog,
    EmailLoginMapping,
    HRSourceRecord,
    OneCImportRun,
)
from app.models_onec_sources import HREmploymentState, OneCAdditionalSource
from app.models_synology import (
    SynologyAccountState,
    SynologyControlSettings,
    SynologyException,
    SynologySyncRun,
)
from app.services.blocking_window import (
    BLOCK_MAX_ATTEMPTS,
    BLOCK_RETRY_MINUTES,
    BLOCK_TIME_LABEL,
    is_block_window_open,
)
from app.services.onec_freshness import OneCSourceFreshnessService
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
from app.services.worker_identity import WorkerIdentityResolver


_SYNC_LOCK = threading.Lock()

# Подтверждение массовой блокировки действует ограниченное время: администратор
# подтверждает конкретный разбор ситуации, а не снимает предохранитель навсегда.
MASS_DISABLE_ACK_TTL_MINUTES = 60

CLASSIFICATION_LABELS = {
    CLASS_INTERNAL_ACTIVE: "Наш сотрудник",
    CLASS_INTERNAL_DISMISSED: "Уволен (блокирует общий контур)",
    CLASS_EXTERNAL: "Внешняя",
    CLASS_UNKNOWN: "Требует классификации",
    CLASS_EXCEPTION: "Исключение",
    CLASS_PROTECTED: "Защищенная DSM",
}

ACTION_LABELS = {
    ACTION_NONE: "–",
    ACTION_CLASSIFY: "Требует классификации",
    # Оставлено для старых значений в БД: политика больше не выдает это
    # действие, нераспознанные учетки теперь отключаются.
    ACTION_MIGRATION_CANDIDATE: "Ожидает 3-месячный цикл",
    ACTION_SET_EXPIRY_INTERNAL: "Запустить 3-месячный цикл",
    ACTION_SET_EXPIRY_EXTERNAL: "Запустить 6-месячный цикл",
    ACTION_DISABLE: "Отключить",
    # Старое значение может остаться в БД до первой новой синхронизации.
    ACTION_DELETE: "Удаление отключено",
}


class SynologyLifecycleService:
    """Сверка локальных DSM-учеток и этап фактической блокировки.

    Текущий write-scope намеренно ограничен одним действием в DSM:
    ``Expired=1``. Удаление и автоматическое включение учеток отсутствуют.
    Календарные 3/6-месячные сроки хранятся в SQLite и по их окончании
    превращаются в действие ``disable``.
    """

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self._identity = WorkerIdentityResolver(db)

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
            return row
        # После установки патча write остается выключен до явного включения в Web.
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
        max_disables_per_run: int,
        write_enabled: bool,
        actor: str,
    ) -> SynologyControlSettings:
        values = {
            "sync_interval_minutes": (sync_interval_minutes, 1, 1440),
            "migration_batch_size": (migration_batch_size, 1, 100),
            "migration_interval_days": (migration_interval_days, 1, 365),
            "internal_expiry_months": (internal_expiry_months, 1, 24),
            "external_expiry_months": (external_expiry_months, 1, 24),
            "delete_after_months": (delete_after_months, 1, 60),
            "max_disables_per_run": (max_disables_per_run, 1, 500),
        }
        for name, (value, minimum, maximum) in values.items():
            if not minimum <= int(value) <= maximum:
                raise ValueError(f"{name}: допустимо от {minimum} до {maximum}")

        row = self.control_settings()
        previous_write = bool(row.write_enabled)
        row.sync_interval_minutes = int(sync_interval_minutes)
        row.migration_batch_size = int(migration_batch_size)
        row.migration_interval_days = int(migration_interval_days)
        row.internal_expiry_months = int(internal_expiry_months)
        row.external_expiry_months = int(external_expiry_months)
        row.delete_after_months = int(delete_after_months)
        previous_limit = int(row.max_disables_per_run or 0)
        row.max_disables_per_run = int(max_disables_per_run)
        row.write_enabled = bool(write_enabled)
        row.updated_by = actor
        row.updated_at = self.utcnow()

        # Повышение лимита не должно молча наследовать старое подтверждение:
        # администратор подтверждал другую по объему ситуацию.
        if previous_limit != row.max_disables_per_run:
            self._clear_mass_disable_ack(row)

        mode = "blocking" if row.write_enabled else "observe"
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
                    f"delete={row.delete_after_months}m; "
                    f"max_disables={row.max_disables_per_run}; mode={mode}"
                ),
            )
        )
        if previous_write != row.write_enabled:
            self.db.add(
                AuditLog(
                    actor=actor,
                    action="synology_blocking_mode_changed",
                    target="Synology DSM",
                    result="enabled" if row.write_enabled else "disabled",
                    details="write_scope=expired_true_only; delete=false; enable=false",
                )
            )
        self.db.commit()
        return row

    def block_window_state(
        self,
        control: SynologyControlSettings,
    ) -> tuple[bool, str]:
        """Можно ли сейчас применять плановые блокировки DSM.

        Окно общее для всего проекта: AD, Zimbra и DSM меняются в одно время
        суток. Внутри окна попытки повторяются с ровным интервалом, пока не
        исчерпан суточный лимит.
        """
        now = self.local_now
        if not is_block_window_open(now):
            return False, f"Плановые блокировки выполняются после {BLOCK_TIME_LABEL}"

        attempts = (
            int(control.block_attempts or 0)
            if control.block_window_date == self.today
            else 0
        )
        if attempts >= BLOCK_MAX_ATTEMPTS:
            return False, (
                f"Исчерпан суточный лимит попыток ({BLOCK_MAX_ATTEMPTS}); "
                "остаток перенесен на следующий вечер"
            )

        last = control.last_block_attempt_at
        if attempts and last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            next_at = last + timedelta(minutes=BLOCK_RETRY_MINUTES)
            if self.utcnow() < next_at:
                return False, (
                    f"Повтор после неудачной попытки через "
                    f"{BLOCK_RETRY_MINUTES} мин"
                )

        return True, "Окно плановых блокировок открыто"

    def _guard_already_logged_today(self) -> bool:
        zone = ZoneInfo(self.settings.app_timezone)
        rows = self.db.scalars(
            select(AuditLog.created_at)
            .where(AuditLog.action == "synology_mass_disable_blocked")
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).all()
        for moment in rows:
            if moment is None:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            if moment.astimezone(zone).date() == self.today:
                return True
        return False

    def _register_block_attempt(self, control: SynologyControlSettings) -> None:
        if control.block_window_date != self.today:
            control.block_window_date = self.today
            control.block_attempts = 0
        control.block_attempts = int(control.block_attempts or 0) + 1
        control.last_block_attempt_at = self.utcnow()

    @staticmethod
    def _clear_mass_disable_ack(row: SynologyControlSettings) -> None:
        row.mass_disable_ack_at = None
        row.mass_disable_ack_count = 0
        row.mass_disable_ack_by = ""

    def _mass_disable_ack_valid_for(
        self,
        control: SynologyControlSettings,
        candidates: int,
    ) -> bool:
        """Проверить, покрывает ли действующее подтверждение текущий объем."""
        moment = control.mass_disable_ack_at
        if moment is None:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        expires_at = moment + timedelta(minutes=MASS_DISABLE_ACK_TTL_MINUTES)
        if self.utcnow() >= expires_at:
            return False
        # Подтверждение действует на объем не больше того, который видел
        # администратор: внезапно выросшая выборка снова требует разбора.
        return candidates <= int(control.mass_disable_ack_count or 0)

    def acknowledge_mass_disable(self, *, actor: str) -> SynologyControlSettings:
        """Разово разрешить блокировку объема, превышающего лимит прогона."""
        control = self.control_settings()
        candidates = self.pending_disable_count()
        if candidates == 0:
            raise ValueError("Нет учетных записей, ожидающих блокировки")
        if candidates <= int(control.max_disables_per_run or 0):
            raise ValueError(
                "Предохранитель не сработал: подтверждение не требуется"
            )

        control.mass_disable_ack_at = self.utcnow()
        control.mass_disable_ack_count = candidates
        control.mass_disable_ack_by = actor
        self._audit(
            action="synology_mass_disable_acknowledged",
            target="Synology DSM",
            result="warning",
            details=(
                f"candidates={candidates}; "
                f"limit={control.max_disables_per_run}; "
                f"ttl_minutes={MASS_DISABLE_ACK_TTL_MINUTES}"
            ),
            actor=actor,
        )
        self.db.commit()
        self.db.refresh(control)
        return control

    def pending_disable_count(self) -> int:
        return int(
            self.db.scalar(
                select(func.count(SynologyAccountState.id)).where(
                    SynologyAccountState.is_present.is_(True),
                    SynologyAccountState.is_active.is_(True),
                    SynologyAccountState.desired_action == ACTION_DISABLE,
                )
            )
            or 0
        )

    def managed_domains(self) -> set[str]:
        domains = {
            normalize_domain(item)
            for item in self.settings.zimbra_domains
            if normalize_domain(item)
        }
        if normalize_domain(self.settings.onec_source_domain):
            domains.add(normalize_domain(self.settings.onec_source_domain))

        rows = self.db.scalars(
            select(OneCAdditionalSource).where(OneCAdditionalSource.enabled.is_(True))
        ).all()
        for row in rows:
            domain = normalize_domain(row.mail_domain)
            if domain:
                domains.add(domain)
        return domains

    def _active_exceptions(
        self,
    ) -> tuple[dict[str, SynologyException], dict[str, SynologyException]]:
        rows = self.db.scalars(
            select(SynologyException).where(SynologyException.is_active.is_(True))
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

    def _hr_snapshot(self, account: SynologyLocalUser) -> dict[str, object]:
        """Связать локальную учетку DSM с работником кадровой выгрузки.

        Правило сопоставления общее для всего проекта и живет в
        WorkerIdentityResolver: e-mail в DSM часто пустой или личный, поэтому
        одного признака недостаточно.
        """
        match = self._identity.resolve(
            emails=[account.email],
            logins=[account.login],
            fio=account.description,
        )
        return {
            "matched": match.matched,
            "active": match.active,
            "worker_key": match.worker_key,
            "match_method": match.method,
            "dismissal_date": match.dismissal_date,
        }

    def _hr_write_ready(self) -> tuple[bool, str]:
        running = bool(
            self.db.scalar(
                select(OneCImportRun.id)
                .where(OneCImportRun.status == "running")
                .limit(1)
            )
        )
        if running:
            return False, "Выполняется импорт 1С"
        ready = OneCSourceFreshnessService(
            self.settings,
            self.db,
        ).all_control_exports_ready(expected_date=self.today)
        if not ready:
            return False, "Нет контрольной выгрузки 1С после 19:00 по всем источникам"
        return True, "Контрольные выгрузки 1С актуальны"

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
            row.lifecycle_started_at,
            row.cycle_started_at,
            row.policy_expires_at,
            row.dismissal_reference_date,
            row.dismissal_reference_source,
            row.delete_after,
            row.desired_action,
            row.desired_reason,
            row.last_action,
            row.last_action_at,
        )

    def _start_cycle(
        self,
        row: SynologyAccountState,
        *,
        months: int,
        now: datetime,
        source: str,
    ) -> None:
        if row.lifecycle_started_at is None:
            row.lifecycle_started_at = now
        row.cycle_started_at = now
        row.policy_expires_at = add_months(self.today, months)
        self._audit(
            action="synology_cycle_started",
            target=row.login,
            details=(
                f"classification={row.classification}; months={months}; "
                f"policy_expires_at={row.policy_expires_at.isoformat()}; source={source}"
            ),
        )

    @staticmethod
    def _clear_cycle(row: SynologyAccountState) -> None:
        row.cycle_started_at = None
        row.policy_expires_at = None

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
        else:
            previous_signature = self._state_signature(row)
            previous_classification = row.classification
            previous_action = row.desired_action
            previous_active = row.last_observed_active

        exception = (
            exception_by_stable.get(account.stable_id)
            or exception_by_login.get(normalize_login(account.login))
        )
        hr = self._hr_snapshot(account)
        classification = classify_account(
            email=account.email,
            managed_domains=managed_domains,
            protected=account.protected,
            exception=exception is not None,
            active_employee=bool(hr["active"]),
            matched_employee=bool(hr["matched"]),
        )

        # worker_key сохраняется при любом успешном сопоставлении, а не только
        # для внутренних классов: по нему общий контур увольнения находит
        # DSM-учетки работника и блокирует их вместе с AD и Zimbra.
        worker_key = str(hr["worker_key"] or "")
        match_method = str(hr["match_method"] or "")

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

        reactivated = previous_active is False and account.is_active

        if classification == CLASS_INTERNAL_ACTIVE:
            row.dismissal_reference_date = None
            row.dismissal_reference_source = ""
            row.delete_after = None
            if reactivated:
                self._start_cycle(
                    row,
                    months=control.internal_expiry_months,
                    now=now,
                    source="reactivation",
                )
        elif classification == CLASS_INTERNAL_DISMISSED:
            self._clear_cycle(row)
            explicit_date = hr["dismissal_date"]
            if explicit_date is not None and row.dismissal_reference_source != "hr":
                row.dismissal_reference_date = explicit_date
                row.dismissal_reference_source = "hr"
                row.delete_after = add_months(explicit_date, control.delete_after_months)
            elif row.dismissal_reference_date is None:
                row.dismissal_reference_date = self.today
                row.dismissal_reference_source = "detected"
                row.delete_after = add_months(self.today, control.delete_after_months)
                self._audit(
                    action="synology_dismissal_detected",
                    target=account.login,
                    details=(
                        f"reference={row.dismissal_reference_date.isoformat()}; "
                        f"source={row.dismissal_reference_source}; "
                        f"delete_after={row.delete_after.isoformat()}"
                    ),
                )
        elif classification == CLASS_EXTERNAL:
            row.dismissal_reference_date = None
            row.dismissal_reference_source = ""
            row.delete_after = None
            if account.is_active and (
                row.cycle_started_at is None
                or row.policy_expires_at is None
                or reactivated
            ):
                self._start_cycle(
                    row,
                    months=control.external_expiry_months,
                    now=now,
                    source="reactivation" if reactivated else "first_seen",
                )
        else:
            row.dismissal_reference_date = None
            row.dismissal_reference_source = ""
            row.delete_after = None
            self._clear_cycle(row)

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

        if reactivated:
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
            previous_action != row.desired_action
            and row.desired_action not in {ACTION_NONE, ACTION_MIGRATION_CANDIDATE}
        ):
            self._audit(
                action="synology_action_required",
                target=account.login,
                result="warning",
                details=f"action={row.desired_action}; reason={row.desired_reason}",
            )

        row.last_observed_active = account.is_active
        row.last_observed_expires_at = account.expires_at

        changed = previous_signature is None or previous_signature != self._state_signature(row)
        return row, is_new, changed

    def _stage_migration_batch(
        self,
        *,
        control: SynologyControlSettings,
        now: datetime,
    ) -> int:
        """Запустить 3-месячный цикл для очередного случайного пакета сотрудников."""
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
        for row in selected:
            self._start_cycle(
                row,
                months=control.internal_expiry_months,
                now=now,
                source="migration_batch",
            )
            row.desired_action = ACTION_NONE
            row.desired_reason = f"Цикл действует до {row.policy_expires_at.isoformat()}."
            self._audit(
                action="synology_migration_selected",
                target=row.login,
                details=(
                    f"policy_expires_at={row.policy_expires_at.isoformat()}; "
                    f"batch_size={count}"
                ),
            )

        control.last_migration_batch_at = now
        return count

    def _refresh_action_for_current_snapshot(
        self,
        row: SynologyAccountState,
        account: SynologyLocalUser,
        *,
        managed_domains: set[str],
        exception_by_login: dict[str, SynologyException],
        exception_by_stable: dict[str, SynologyException],
        control: SynologyControlSettings,
    ) -> str:
        exception = (
            exception_by_stable.get(account.stable_id)
            or exception_by_login.get(normalize_login(account.login))
        )
        hr = self._hr_snapshot(account)
        classification = classify_account(
            email=account.email,
            managed_domains=managed_domains,
            protected=account.protected,
            exception=exception is not None,
            active_employee=bool(hr["active"]),
            matched_employee=bool(hr["matched"]),
        )
        if classification != row.classification:
            row.classification = classification
            row.worker_key = str(hr["worker_key"] or "") if classification in {
                CLASS_INTERNAL_ACTIVE,
                CLASS_INTERNAL_DISMISSED,
            } else ""
            row.match_method = str(hr["match_method"] or "") if classification in {
                CLASS_INTERNAL_ACTIVE,
                CLASS_INTERNAL_DISMISSED,
            } else ""

        decision = desired_action(
            classification=classification,
            is_active=account.is_active,
            observed_expires_at=account.expires_at,
            today=self.today,
            delete_after=row.delete_after,
            enrolled=row.cycle_started_at is not None,
            policy_expires_at=row.policy_expires_at,
            previous_active=row.last_observed_active,
            internal_months=control.internal_expiry_months,
            external_months=control.external_expiry_months,
        )
        row.desired_action = decision.action
        row.desired_reason = decision.reason
        return classification

    def _execute_disables(
        self,
        *,
        accounts: list[SynologyLocalUser],
        control: SynologyControlSettings,
        managed_domains: set[str],
        exception_by_login: dict[str, SynologyException],
        exception_by_stable: dict[str, SynologyException],
    ) -> tuple[int, int, int, str]:
        """Исполнить только Expired=1.

        Возвращает ``success, failed, deferred, guard_message``. Непустой
        ``guard_message`` означает, что этап блокировок не выполнялся вообще.
        """
        if not control.write_enabled:
            return 0, 0, 0, ""

        by_stable = {account.stable_id: account for account in accounts}
        rows = list(
            self.db.scalars(
                select(SynologyAccountState).where(
                    SynologyAccountState.is_present.is_(True),
                    SynologyAccountState.is_active.is_(True),
                    SynologyAccountState.desired_action == ACTION_DISABLE,
                )
            ).all()
        )
        if not rows:
            return 0, 0, 0, ""

        # Единое окно проекта. Работа никуда не девается: невыполненное
        # останется в desired_action и уйдет в ближайшую разрешенную попытку.
        window_open, _window_reason = self.block_window_state(control)
        if not window_open:
            return 0, 0, len(rows), ""

        # Предохранитель: единичная ошибка кадровой выгрузки не должна
        # превратиться в массовое отключение. Частичное исполнение здесь тоже
        # недопустимо, поэтому этап пропускается целиком.
        limit = max(1, int(control.max_disables_per_run or 1))
        if len(rows) > limit:
            if not self._mass_disable_ack_valid_for(control, len(rows)):
                guard = (
                    f"Сработал предохранитель массовой блокировки: "
                    f"к отключению {len(rows)} учеток при лимите {limit}. "
                    f"Блокировки не выполнялись. Проверьте кадровые данные и "
                    f"подтвердите операцию вручную."
                )
                # Пока предохранитель не снят, ситуация повторяется на каждой
                # сверке. В журнал она попадает один раз за сутки, иначе
                # вечерний журнал заполнялся бы одной и той же записью.
                if not self._guard_already_logged_today():
                    self._audit(
                        action="synology_mass_disable_blocked",
                        target="Synology DSM",
                        result="warning",
                        details=f"candidates={len(rows)}; limit={limit}",
                    )
                    self.db.commit()
                return 0, 0, len(rows), guard
            self._audit(
                action="synology_mass_disable_confirmed",
                target="Synology DSM",
                result="warning",
                details=(
                    f"candidates={len(rows)}; limit={limit}; "
                    f"actor={control.mass_disable_ack_by or '-'}"
                ),
            )
            # Подтверждение одноразовое: следующий превышающий лимит прогон
            # снова потребует разбора.
            self._clear_mass_disable_ack(control)
            self.db.commit()

        # Попытка засчитывается до обращения к DSM: если сервер недоступен и
        # упадут все учетки, следующий заход будет ровно через интервал
        # повтора, а не на каждом тике планировщика.
        self._register_block_attempt(control)
        self.db.commit()

        dsm = SynologyService(self.settings)
        success = failed = deferred = 0

        for row in rows:
            account = by_stable.get(row.stable_id)
            if account is None or account.detail_error:
                failed += 1
                row.last_error = account.detail_error if account is not None else "DSM: учетка исчезла из снимка"
                continue

            classification = self._refresh_action_for_current_snapshot(
                row,
                account,
                managed_domains=managed_domains,
                exception_by_login=exception_by_login,
                exception_by_stable=exception_by_stable,
                control=control,
            )
            if row.desired_action != ACTION_DISABLE:
                deferred += 1
                continue
            # Последний рубеж перед изменением DSM. CLASS_UNKNOWN здесь
            # обязателен: нераспознанная учетка может принадлежать
            # действующему работнику, у которого просто не заполнен e-mail.
            if classification in {
                CLASS_EXCEPTION,
                CLASS_PROTECTED,
                CLASS_UNKNOWN,
            }:
                deferred += 1
                continue
            if classification in {CLASS_INTERNAL_ACTIVE, CLASS_INTERNAL_DISMISSED}:
                # Проверка выполняется непосредственно перед каждым внешним
                # изменением: импорт мог начаться уже после начала DSM-sync.
                hr_ready, hr_reason = self._hr_write_ready()
                if not hr_ready:
                    deferred += 1
                    # Это штатное ожидание, не ошибка карточки.
                    row.desired_reason = f"{row.desired_reason} Ожидание: {hr_reason}."
                    continue

            try:
                after = dsm.expire_account(account)
                row.status = after.status
                row.is_active = after.is_active
                row.expires_at = after.expires_at
                row.last_observed_active = False
                row.last_action = "disable"
                row.last_action_at = self.utcnow()
                row.last_error = ""
                row.desired_action = ACTION_NONE
                row.desired_reason = "Учетная запись отключена в DSM (Expired=1)."
                self._audit(
                    action="synology_account_disabled",
                    target=row.login,
                    details=(
                        f"classification={classification}; email={row.email}; "
                        "method=Expired=1"
                    ),
                )
                success += 1
            except Exception as exc:
                failed += 1
                row.last_error = str(exc)
                self._audit(
                    action="synology_disable_failed",
                    target=row.login,
                    result="error",
                    details=str(exc),
                )

            # Изменение в DSM уже произошло и откатить его нельзя. Фиксируем
            # результат сразу, иначе ошибка на следующей учетке откатила бы
            # журнал и состояние уже отключенных записей.
            self.db.commit()

        return success, failed, deferred, ""

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

            self._stage_migration_batch(control=control, now=now)

            for row in states:
                if row.id in seen_ids:
                    continue
                if previously_present.get(row.id, False):
                    row.desired_action = ACTION_NONE
                    row.desired_reason = "Учетная запись отсутствует в текущем списке DSM."

            # Наблюдение фиксируется до внешних изменений: дальше каждая
            # успешная блокировка коммитится отдельно и не может быть потеряна
            # откатом общей транзакции прогона.
            self.db.commit()

            disabled, disable_errors, deferred, guard_message = self._execute_disables(
                accounts=accounts,
                control=control,
                managed_domains=domains,
                exception_by_login=by_login,
                exception_by_stable=by_stable,
            )
            detail_errors += disable_errors

            planned = int(
                self.db.scalar(
                    select(func.count(SynologyAccountState.id)).where(
                        SynologyAccountState.is_present.is_(True),
                        SynologyAccountState.desired_action != ACTION_NONE,
                    )
                )
                or 0
            )

            run.users_count = len(accounts)
            run.new_accounts = new_count
            run.changed_accounts = changed_count
            run.planned_actions = planned
            run.detail_errors = detail_errors
            run.disabled_accounts = disabled
            run.guard_message = guard_message
            run.status = "partial" if detail_errors or guard_message else "success"
            run.message = (
                f"users={len(accounts)}; new={new_count}; changed={changed_count}; "
                f"planned={planned}; disabled={disabled}; deferred={deferred}; "
                f"detail_errors={detail_errors}; "
                f"guard={'triggered' if guard_message else 'ok'}; "
                f"mode={'blocking' if control.write_enabled else 'observe'}"
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

    def recent_disabled(self, *, limit: int = 200) -> list[dict[str, object]]:
        """Учетки, отключенные автоматикой, для разбора и отката."""
        rows = list(
            self.db.scalars(
                select(SynologyAccountState)
                .where(
                    SynologyAccountState.last_action == "disable",
                    SynologyAccountState.is_active.is_(False),
                )
                .order_by(SynologyAccountState.last_action_at.desc())
                .limit(max(1, min(int(limit), 2000)))
            ).all()
        )
        return [
            {
                "row": row,
                "classification_label": CLASSIFICATION_LABELS.get(
                    row.classification, row.classification
                ),
            }
            for row in rows
        ]

    def restore_account(self, state_id: int, *, actor: str) -> SynologyAccountState:
        """Вернуть ошибочно заблокированную учетку и вывести ее из-под цикла.

        Одного Expired=0 мало: если оставить прежнюю классификацию, ближайшая
        сверка отключит учетку снова. Поэтому запись переводится в состояние,
        требующее ручного решения, и цикл сбрасывается.
        """
        row = self.db.get(SynologyAccountState, int(state_id))
        if row is None:
            raise ValueError("Учетная запись не найдена")

        after = SynologyService(self.settings).restore_account(row.login)

        row.status = after.status
        row.is_active = after.is_active
        row.expires_at = after.expires_at
        row.last_observed_active = True
        row.last_action = "restore"
        row.last_action_at = self.utcnow()
        row.last_error = ""
        self._clear_cycle(row)
        row.classification = CLASS_UNKNOWN
        row.desired_action = ACTION_CLASSIFY
        row.desired_reason = (
            f"Восстановлена вручную ({actor}). Автоматические действия "
            "приостановлены до явного решения администратора."
        )
        self._audit(
            action="synology_account_restored",
            target=row.login,
            result="warning",
            details=f"stable_id={row.stable_id}; email={row.email or '-'}",
            actor=actor,
        )
        self.db.commit()
        self.db.refresh(row)
        return row

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

        conditions = [func.lower(SynologyAccountState.login) == normalized]
        if row.stable_id:
            conditions.append(SynologyAccountState.stable_id == row.stable_id)
        states = list(
            self.db.scalars(
                select(SynologyAccountState).where(or_(*conditions))
            ).all()
        )
        for state in states:
            state.classification = CLASS_EXCEPTION
            self._clear_cycle(state)
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

    def connection_view(
        self,
        control: SynologyControlSettings | None = None,
    ) -> dict[str, object]:
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
        control = control or self.control_settings()
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
            "mode": "Блокировка (Expired=1)" if control.write_enabled else "Наблюдение",
        }

    def view(self, *, limit: int = 2000) -> dict[str, object]:
        control = self.control_settings()
        latest = self.db.scalar(
            select(SynologySyncRun).order_by(SynologySyncRun.id.desc()).limit(1)
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
            account_rows.append(
                {
                    "row": row,
                    "classification_label": CLASSIFICATION_LABELS.get(
                        row.classification, row.classification
                    ),
                    "action_label": ACTION_LABELS.get(
                        row.desired_action, row.desired_action
                    ),
                    "proposed_expiry": row.policy_expires_at,
                    "linked": bool(row.worker_key),
                }
            )

        hr_ready, hr_reason = self._hr_write_ready()
        window_open, window_reason = self.block_window_state(control)
        pending_disables = sum(
            1 for row in states if row.desired_action == ACTION_DISABLE and row.is_active
        )
        limit = max(1, int(control.max_disables_per_run or 1))
        guard_triggered = (
            control.write_enabled
            and pending_disables > limit
            and not self._mass_disable_ack_valid_for(control, pending_disables)
        )
        return {
            "connection": self.connection_view(control),
            "control": control,
            "latest": latest,
            "runs": runs,
            "accounts": account_rows,
            "disabled_recent": self.recent_disabled(),
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
            "observe_only": not control.write_enabled,
            "hr_write_ready": hr_ready,
            "hr_write_reason": hr_reason,
            "pending_disables": pending_disables,
            "disable_limit": limit,
            "window_open": window_open,
            "window_reason": window_reason,
            "window_label": BLOCK_TIME_LABEL,
            "window_attempts": (
                int(control.block_attempts or 0)
                if control.block_window_date == self.today
                else 0
            ),
            "window_max_attempts": BLOCK_MAX_ATTEMPTS,
            "window_retry_minutes": BLOCK_RETRY_MINUTES,
            "guard_triggered": guard_triggered,
            "guard_ack_valid": self._mass_disable_ack_valid_for(
                control, pending_disables
            ),
            "guard_ack_ttl_minutes": MASS_DISABLE_ACK_TTL_MINUTES,
        }
