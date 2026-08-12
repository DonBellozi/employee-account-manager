from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import OneCImportRun
from app.models_zimbra_lifecycle import ZimbraLifecycleRun
from app.models_zimbra_observer import ZimbraLifecycleState, ZimbraObservationRun
from app.services.zimbra_lifecycle import ZimbraLifecycleService
from app.services.zimbra_observer import as_utc
from app.services.zimbra_protection import ManagedZimbraObserverService


MAX_OBSERVATION_AGE_MINUTES = 15


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ZimbraScheduledLifecycleExecutor:
    """Исполняет результаты только что завершенной плановой проверки Zimbra.

    Повторный полный ``gaa -v`` не запускается. Вместо этого перед изменениями
    выполняются легкие защитные проверки: кадровый импорт не должен идти,
    объединенный HR-снимок должен оставаться свежим, активные адреса 1С и
    Web-защита проверяются повторно, а фактический статус Zimbra все равно
    перечитывается существующим исполнителем непосредственно перед действием.
    """

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self.lifecycle = ZimbraLifecycleService(settings, db)

    def _import_running(self) -> bool:
        return bool(
            self.db.scalar(
                select(OneCImportRun.id)
                .where(OneCImportRun.status == "running")
                .limit(1)
            )
        )

    @staticmethod
    def _state_addresses(state: ZimbraLifecycleState) -> set[str]:
        result = {str(state.primary_email or "").strip().lower()}
        try:
            raw = json.loads(state.addresses_json or "[]")
        except (TypeError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, list):
            result.update(str(item or "").strip().lower() for item in raw)
        result.discard("")
        return result

    def _observation(self, observation_run_id: int) -> ZimbraObservationRun:
        row = self.db.get(ZimbraObservationRun, int(observation_run_id))
        if row is None:
            raise RuntimeError("Плановая проверка Zimbra не найдена")
        if row.trigger != "scheduled":
            raise RuntimeError("Автоисполнение разрешено только после плановой проверки")
        if row.status != "success":
            raise RuntimeError(
                "Автоисполнение разрешено только после успешной плановой проверки"
            )
        completed_at = as_utc(row.completed_at)
        if completed_at is None:
            raise RuntimeError("Плановая проверка Zimbra не завершена")
        if utcnow() - completed_at > timedelta(minutes=MAX_OBSERVATION_AGE_MINUTES):
            raise RuntimeError("Плановая проверка Zimbra устарела для автоисполнения")
        return row

    @staticmethod
    def _allowed_recommendations(config) -> set[str]:
        allowed: set[str] = set()
        if config.allow_close:
            allowed.add("close")
        # В автоматическом режиме backup без удаления не запускаем: иначе один
        # и тот же closed-ящик создавал бы новый TGZ каждый день. Backup+delete
        # является единым автоматическим этапом и требует обоих разрешений.
        if config.allow_backup and config.allow_delete:
            allowed.add("archive_delete")
        return allowed

    def _eligible_states(self, allowed: set[str]) -> list[ZimbraLifecycleState]:
        if not allowed:
            return []
        return [
            state
            for state in self.lifecycle._actionable_states()
            if state.recommendation in allowed
        ]

    def execute_from_observation(
        self,
        observation_run_id: int,
    ) -> ZimbraLifecycleRun | None:
        observation = self._observation(observation_run_id)
        config = self.lifecycle.get_settings_record()
        allowed = self._allowed_recommendations(config)
        states = self._eligible_states(allowed)
        if not states:
            return None

        if self._import_running():
            raise RuntimeError("Автоисполнение Zimbra отложено: идет импорт 1С")

        hr = ManagedZimbraObserverService(self.settings, self.db)._hr_snapshot()
        if not hr.fresh:
            raise RuntimeError("Автоисполнение Zimbra отменено: кадровые данные неактуальны")

        if not self.lifecycle._execution_lock.acquire(blocking=False):
            raise RuntimeError("Исполнение жизненного цикла Zimbra уже выполняется")

        run = None
        try:
            run = self.lifecycle._start_run("execute", "system")
            run.trigger = "scheduled"
            run.observation_run_id = observation.id
            run.planned_close = sum(s.recommendation == "close" for s in states)
            run.planned_archive = sum(
                s.recommendation == "archive_delete" for s in states
            )
            self.db.commit()

            for state in states:
                # Кадровый снимок перечитан уже после наблюдения. Если адрес за
                # это время появился среди действующих работников, ничего не
                # меняем даже при старой рекомендации в ZimbraLifecycleState.
                if self._state_addresses(state) & set(hr.emails):
                    self.lifecycle._add_action(
                        run,
                        state,
                        action="skip",
                        status="blocked",
                        message=(
                            "Автодействие отменено: учетная запись защищена "
                            "актуальными кадровыми данными 1С."
                        ),
                    )
                    run.skipped_count += 1
                    self.db.commit()
                    continue

                if self.lifecycle._is_web_protected(state.zimbra_id):
                    self.lifecycle._add_action(
                        run,
                        state,
                        action="skip",
                        status="blocked",
                        message=(
                            "Автодействие отменено: перед исполнением обнаружена "
                            "активная Web-защита учетной записи."
                        ),
                    )
                    run.skipped_count += 1
                    self.db.commit()
                    continue

                if self.settings.dry_run:
                    self.lifecycle._add_action(
                        run,
                        state,
                        action=(
                            "close"
                            if state.recommendation == "close"
                            else "backup_delete"
                        ),
                        status="planned",
                        message="APP DRY_RUN включен: автоматическое действие не выполнялось.",
                    )
                    run.skipped_count += 1
                    self.db.commit()
                    continue

                if state.recommendation == "close":
                    self.lifecycle._execute_close(run, state, config)
                elif state.recommendation == "archive_delete":
                    self.lifecycle._execute_archive(run, state, config)

            return self.lifecycle._finish_run(run)
        except Exception as exc:
            self.db.rollback()
            if run is not None:
                current = self.db.get(ZimbraLifecycleRun, run.id)
                if current is not None:
                    current.status = "failed"
                    current.error_message = str(exc)[:4000]
                    current.completed_at = utcnow()
                    self.db.commit()
                    self.db.refresh(current)
                    return current
            raise
        finally:
            self.lifecycle._execution_lock.release()
