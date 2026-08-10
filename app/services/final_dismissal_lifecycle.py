from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    EmailLoginMapping,
    HRSourceRecord,
    OneCImportRun,
)
from app.models_dismissal_lifecycle import (
    FinalDismissalAutomationState,
    FinalDismissalBlockRun,
    FinalDismissalBlockTarget,
)
from app.services.ad import ActiveDirectoryService
from app.services.onec_freshness import OneCSourceFreshnessService
from app.services.upcoming_dismissals import UpcomingDismissalService
from app.services.zimbra import ZimbraService


logger = logging.getLogger(__name__)

POLL_SECONDS = 60
BLOCK_TIME = time(19, 10)
BLOCK_TIME_LABEL = "19:10"
SUCCESS_TARGET_STATUSES = {"completed", "already_completed"}
RETRY_DELAYS_MINUTES = (1, 2, 5, 10, 15, 30, 60)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


class PermanentLifecycleError(RuntimeError):
    pass


class FinalDismissalLifecycleService:
    """Боевая автоблокировка после окончательного увольнения.

    Перед каждым внешним изменением проверяется:
    - нет ли выполняющегося импорта 1С;
    - свежи ли все включенные кадровые источники;
    - человек все еще окончательно увольняется;
    - не появилась ли отсрочка;
    - наступила ли дата и время блокировки.

    Старые увольнения до даты включения автоматики не догоняются автоматически.
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

    def _automation_state(self) -> FinalDismissalAutomationState:
        row = self.db.get(FinalDismissalAutomationState, 1)
        if row is not None:
            return row

        now = self.local_now
        row = FinalDismissalAutomationState(
            id=1,
            activated_on=now.date(),
            activated_at=utcnow(),
        )
        self.db.add(row)
        self.db.add(
            AuditLog(
                actor="system",
                action="final_dismissal_automation_armed",
                target="AD+Zimbra",
                result="enabled",
                details=(
                    f"activated_on={now.date().isoformat()}; "
                    f"block_time={BLOCK_TIME_LABEL}; "
                    "historical_backfill=false"
                ),
            )
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def _import_running(self) -> bool:
        return bool(
            self.db.scalar(
                select(OneCImportRun.id)
                .where(OneCImportRun.status == "running")
                .limit(1)
            )
        )

    def _sources_synchronized(self) -> bool:
        # После 19:10 внешние изменения разрешаются только если по КАЖДОМУ
        # включенному источнику IMAP-worker уже принял контрольное письмо
        # текущего дня, отправленное не ранее 19:00.
        return OneCSourceFreshnessService(
            self.settings,
            self.db,
        ).all_control_exports_ready(expected_date=self.today)

    def _ready(self) -> bool:
        return not self._import_running() and self._sources_synchronized()

    @staticmethod
    def _is_temporary_error(exc: Exception) -> bool:
        text = str(exc or "").casefold()
        permanent_markers = (
            "не найд",
            "not found",
            "no such account",
            "invalid credential",
            "authentication failed",
            "permission denied",
            "access denied",
            "insufficient access",
            "недостаточно прав",
            "неверный пароль",
            "host key",
            "fingerprint",
            "bind failed",
            "bind не выполнен",
        )
        if any(marker in text for marker in permanent_markers):
            return False

        temporary_markers = (
            "timeout",
            "timed out",
            "connection refused",
            "connection reset",
            "connection aborted",
            "unable to connect",
            "cannot connect",
            "could not connect",
            "server unavailable",
            "service unavailable",
            "network is unreachable",
            "no route to host",
            "name or service not known",
            "temporary failure in name resolution",
            "connection closed",
            "недоступ",
            "таймаут",
            "время ожидания",
        )
        return any(marker in text for marker in temporary_markers)

    @staticmethod
    def _next_retry(attempts: int) -> datetime:
        index = max(
            0,
            min(attempts - 1, len(RETRY_DELAYS_MINUTES) - 1),
        )
        return utcnow() + timedelta(
            minutes=RETRY_DELAYS_MINUTES[index]
        )

    def _candidate_map(self) -> dict[tuple[str, date], dict]:
        service = UpcomingDismissalService(self.settings, self.db)
        return {
            (item["worker_key"], item["dismissal_date"]): item
            for item in service.list_for_blocking(limit=10000)
        }

    @staticmethod
    def _due(candidate: dict, now: datetime) -> bool:
        effective_date = candidate["effective_block_date"]
        if effective_date < now.date():
            return True
        if effective_date > now.date():
            return False
        return now.time().replace(tzinfo=None) >= BLOCK_TIME

    def _still_due(
        self,
        *,
        worker_key: str,
        dismissal_date: date,
    ) -> dict | None:
        if not self._ready():
            return None
        candidate = self._candidate_map().get(
            (worker_key, dismissal_date)
        )
        if candidate is None:
            return None
        if not self._due(candidate, self.local_now):
            return None
        return candidate

    def _records_and_mappings(
        self,
        worker_key: str,
    ) -> tuple[list[HRSourceRecord], list[EmailLoginMapping]]:
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key == worker_key
                )
            ).all()
        )
        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == worker_key
                )
            ).all()
        )
        return records, mappings

    @staticmethod
    def _ad_plan(
        records: list[HRSourceRecord],
        mappings: list[EmailLoginMapping],
    ) -> dict | None:
        guids = {
            normalize(mapping.ad_object_guid)
            for mapping in mappings
            if normalize(mapping.ad_object_guid)
        }
        if len(guids) > 1:
            return {
                "system": "ad",
                "target_key": "ad:conflict",
                "identifier": "",
                "stable_id": "",
                "error": "У одного человека найдены разные AD objectGUID",
            }

        logins = {
            normalize(mapping.ad_login)
            for mapping in mappings
            if normalize(mapping.ad_login)
        }
        logins.update(
            normalize(record.login)
            for record in records
            if normalize(record.login)
        )

        if len(guids) == 1:
            guid = next(iter(guids))
            login = next(iter(sorted(logins)), "")
            return {
                "system": "ad",
                "target_key": f"ad:{guid}",
                "identifier": login,
                "stable_id": guid,
                "error": "",
            }

        if len(logins) > 1:
            return {
                "system": "ad",
                "target_key": "ad:conflict",
                "identifier": "",
                "stable_id": "",
                "error": "У одного человека найдены разные логины AD",
            }
        if len(logins) == 1:
            login = next(iter(logins))
            return {
                "system": "ad",
                "target_key": f"ad:{login}",
                "identifier": login,
                "stable_id": "",
                "error": "",
            }
        return None

    @staticmethod
    def _zimbra_plan(
        records: list[HRSourceRecord],
        mappings: list[EmailLoginMapping],
    ) -> list[dict]:
        # Стабильный zimbraId является физическим ящиком. Если он известен,
        # алиасы разных организаций объединяются в одну цель.
        by_id: dict[str, set[str]] = defaultdict(set)
        mapped_addresses: set[str] = set()

        for mapping in mappings:
            zimbra_id = normalize(mapping.zimbra_id)
            addresses = {
                normalize(mapping.source_email),
                normalize(mapping.zimbra_email),
            }
            addresses.discard("")
            mapped_addresses.update(addresses)
            if zimbra_id:
                by_id[zimbra_id].update(addresses)

        result: list[dict] = []
        for zimbra_id, addresses in sorted(by_id.items()):
            identifier = next(iter(sorted(addresses)), "")
            result.append(
                {
                    "system": "zimbra",
                    "target_key": f"zimbra:{zimbra_id}",
                    "identifier": identifier,
                    "stable_id": zimbra_id,
                    "error": "",
                }
            )

        # Адрес без zimbraId тоже является допустимой целью. При выполнении
        # фактический zimbraId будет считан и сохранен в строке.
        seen = {
            address
            for addresses in by_id.values()
            for address in addresses
        }
        raw_addresses = {
            normalize(record.corporate_email)
            for record in records
            if normalize(record.corporate_email)
        }
        raw_addresses.update(
            address
            for address in mapped_addresses
            if address
        )

        for address in sorted(raw_addresses - seen):
            result.append(
                {
                    "system": "zimbra",
                    "target_key": f"zimbra-email:{address}",
                    "identifier": address,
                    "stable_id": "",
                    "error": "",
                }
            )

        return result

    def _ensure_run(
        self,
        candidate: dict,
    ) -> FinalDismissalBlockRun:
        run = self.db.scalar(
            select(FinalDismissalBlockRun).where(
                FinalDismissalBlockRun.worker_key
                == candidate["worker_key"],
                FinalDismissalBlockRun.dismissal_date
                == candidate["dismissal_date"],
            )
        )
        if run is None:
            run = FinalDismissalBlockRun(
                worker_key=candidate["worker_key"],
                dismissal_date=candidate["dismissal_date"],
                effective_block_date=candidate["effective_block_date"],
                fio=str(candidate.get("fio") or ""),
                status="pending",
            )
            self.db.add(run)
            self.db.flush()
        else:
            run.effective_block_date = candidate[
                "effective_block_date"
            ]
            run.fio = str(candidate.get("fio") or run.fio)

        records, mappings = self._records_and_mappings(
            candidate["worker_key"]
        )
        plans: list[dict] = []
        ad = self._ad_plan(records, mappings)
        if ad is not None:
            plans.append(ad)
        plans.extend(self._zimbra_plan(records, mappings))

        for plan in plans:
            existing = self.db.scalar(
                select(FinalDismissalBlockTarget).where(
                    FinalDismissalBlockTarget.run_id == run.id,
                    FinalDismissalBlockTarget.system == plan["system"],
                    FinalDismissalBlockTarget.target_key
                    == plan["target_key"],
                )
            )
            if existing is not None:
                if plan["identifier"]:
                    existing.target_identifier = plan["identifier"]
                if plan["stable_id"]:
                    existing.stable_id = plan["stable_id"]
                continue

            status = "intervention" if plan["error"] else "pending"
            self.db.add(
                FinalDismissalBlockTarget(
                    run_id=run.id,
                    system=plan["system"],
                    target_key=plan["target_key"],
                    target_identifier=plan["identifier"],
                    stable_id=plan["stable_id"],
                    status=status,
                    last_error=plan["error"],
                    next_attempt_at=(
                        None if plan["error"] else utcnow()
                    ),
                )
            )

        if not plans:
            run.status = "intervention"
            run.last_error = (
                "Не найден ни один связанный объект AD/Zimbra"
            )

        self.db.commit()
        self.db.refresh(run)
        return run

    def _targets(
        self,
        run_id: int,
    ) -> list[FinalDismissalBlockTarget]:
        return list(
            self.db.scalars(
                select(FinalDismissalBlockTarget)
                .where(
                    FinalDismissalBlockTarget.run_id == run_id
                )
                .order_by(
                    FinalDismissalBlockTarget.system,
                    FinalDismissalBlockTarget.id,
                )
            ).all()
        )

    @staticmethod
    def _target_due(
        target: FinalDismissalBlockTarget,
        now: datetime,
    ) -> bool:
        if target.status != "pending":
            return False
        if target.next_attempt_at is None:
            return True
        due = target.next_attempt_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return due <= now

    def _complete_target(
        self,
        target: FinalDismissalBlockTarget,
        *,
        status: str,
        result: str,
    ) -> None:
        target.status = status
        target.last_result = result
        target.last_error = ""
        target.next_attempt_at = None
        target.completed_at = utcnow()
        target.updated_at = utcnow()

    def _fail_target(
        self,
        target: FinalDismissalBlockTarget,
        exc: Exception,
    ) -> None:
        target.last_error = str(exc)[:4000]
        target.last_result = "error"
        target.completed_at = None
        target.updated_at = utcnow()
        if (
            isinstance(exc, PermanentLifecycleError)
            or not self._is_temporary_error(exc)
        ):
            target.status = "intervention"
            target.next_attempt_at = None
        else:
            target.status = "pending"
            target.next_attempt_at = self._next_retry(
                target.attempts
            )

    def _process_ad(
        self,
        target: FinalDismissalBlockTarget,
    ) -> None:
        service = ActiveDirectoryService(self.settings)
        user = None
        if normalize(target.stable_id):
            user = service.get_user_by_object_guid(
                normalize(target.stable_id)
            )
        if user is None and normalize(target.target_identifier):
            user = service.get_user(
                normalize(target.target_identifier)
            )
        if user is None:
            raise PermanentLifecycleError(
                "Учетная запись AD не найдена"
            )

        target.target_identifier = user.username
        if user.object_guid:
            target.stable_id = user.object_guid

        if not user.is_enabled:
            self._complete_target(
                target,
                status="already_completed",
                result="already_disabled",
            )
            return

        service.disable_user(user.username)
        self._complete_target(
            target,
            status="completed",
            result="disabled",
        )

    def _process_zimbra(
        self,
        target: FinalDismissalBlockTarget,
    ) -> None:
        service = ZimbraService(self.settings)
        identity = None
        if normalize(target.stable_id):
            identity = service.accounts_by_ids(
                [normalize(target.stable_id)]
            ).get(normalize(target.stable_id))
        if identity is None and normalize(
            target.target_identifier
        ):
            identity = service.account_by_address(
                normalize(target.target_identifier)
            )
        if identity is None:
            raise PermanentLifecycleError(
                "Учетная запись Zimbra не найдена"
            )

        target.target_identifier = identity.primary_email
        if identity.zimbra_id:
            target.stable_id = identity.zimbra_id

        if normalize(identity.account_status) == "closed":
            self._complete_target(
                target,
                status="already_completed",
                result="already_closed",
            )
            return

        service.close_account(identity.primary_email)
        self._complete_target(
            target,
            status="completed",
            result="closed",
        )

    def _refresh_run(
        self,
        run: FinalDismissalBlockRun,
    ) -> None:
        targets = self._targets(run.id)
        if not targets:
            run.status = "intervention"
            run.last_error = (
                run.last_error
                or "Для увольнения не найдены объекты AD/Zimbra"
            )
            run.completed_at = None
            return

        completed = [
            item
            for item in targets
            if item.status in SUCCESS_TARGET_STATUSES
        ]
        pending = [
            item for item in targets if item.status == "pending"
        ]
        intervention = [
            item
            for item in targets
            if item.status == "intervention"
        ]
        cancelled = [
            item
            for item in targets
            if item.status == "cancelled"
        ]

        if len(cancelled) == len(targets):
            run.status = "cancelled"
            run.cancelled_at = run.cancelled_at or utcnow()
            run.completed_at = None
        elif pending:
            run.status = (
                "partial"
                if completed or intervention
                else "running"
            )
            run.completed_at = None
        elif intervention:
            run.status = (
                "partial" if completed else "intervention"
            )
            run.completed_at = None
        else:
            run.status = "success"
            run.completed_at = utcnow()
            run.cancelled_at = None

        errors = [
            (
                ("AD" if item.system == "ad" else "Zimbra")
                + ": "
                + item.last_error
            )
            for item in targets
            if item.last_error
            and item.status in {"pending", "intervention"}
        ]
        run.last_error = "\n".join(errors)[:4000]
        run.updated_at = utcnow()

    def _cancel_run(
        self,
        run: FinalDismissalBlockRun,
        reason: str,
    ) -> None:
        # Завершенные внешние действия намеренно не откатываются.
        # Отменяются только еще не исполненные цели.
        for target in self._targets(run.id):
            if target.status in {"pending", "intervention"}:
                target.status = "cancelled"
                target.next_attempt_at = None
                target.last_error = reason
                target.updated_at = utcnow()
        run.cancelled_at = utcnow()
        run.last_error = reason
        self._refresh_run(run)
        self.db.commit()

    def _process_target(
        self,
        run: FinalDismissalBlockRun,
        target: FinalDismissalBlockTarget,
    ) -> None:
        # Последний interlock прямо перед внешним изменением.
        candidate = self._still_due(
            worker_key=run.worker_key,
            dismissal_date=run.dismissal_date,
        )
        if candidate is None:
            self._cancel_run(
                run,
                "Кадровая ситуация или дата блокировки изменилась",
            )
            return

        target.attempts = int(target.attempts or 0) + 1
        target.last_attempt_at = utcnow()
        target.updated_at = utcnow()

        try:
            if target.system == "ad":
                self._process_ad(target)
            elif target.system == "zimbra":
                self._process_zimbra(target)
            else:
                raise PermanentLifecycleError(
                    f"Неизвестная система: {target.system}"
                )
        except Exception as exc:
            self._fail_target(target, exc)
        self.db.commit()

    def process(self) -> dict[str, int | str]:
        state = self._automation_state()

        if self.settings.dry_run:
            return {
                "status": "dry_run",
                "runs": 0,
                "targets": 0,
            }
        if not self._ready():
            return {
                "status": "sources_not_ready",
                "runs": 0,
                "targets": 0,
            }

        candidate_map = self._candidate_map()

        # Если человек снова стал активным либо дата изменилась до
        # исполнения, отменяем только еще не выполненные действия.
        open_runs = list(
            self.db.scalars(
                select(FinalDismissalBlockRun).where(
                    FinalDismissalBlockRun.status.in_(
                        ["pending", "running", "partial", "intervention"]
                    )
                )
            ).all()
        )
        for run in open_runs:
            candidate = candidate_map.get(
                (run.worker_key, run.dismissal_date)
            )
            if candidate is None:
                self._cancel_run(
                    run,
                    "Работник больше не является окончательно увольняющимся",
                )
                continue
            run.effective_block_date = candidate[
                "effective_block_date"
            ]
            if not self._due(candidate, self.local_now):
                # Отсрочка могла быть установлена уже после формирования run.
                continue

        eligible = [
            item
            for item in candidate_map.values()
            if item["dismissal_date"] >= state.activated_on
            and self._due(item, self.local_now)
        ]
        eligible.sort(
            key=lambda item: (
                item["effective_block_date"],
                str(item.get("fio") or "").casefold(),
            )
        )

        runs_touched = 0
        targets_processed = 0
        for candidate in eligible:
            run = self._ensure_run(candidate)
            if run.status == "cancelled":
                continue
            runs_touched += 1

            now = utcnow()
            for target in self._targets(run.id):
                if not self._target_due(target, now):
                    continue
                self._process_target(run, target)
                targets_processed += 1
                # Один target за раз: перед следующей системой заново
                # проверяем кадровое состояние и отсутствие активного импорта.
                if run.status == "cancelled":
                    break

            self._refresh_run(run)
            self.db.commit()

        return {
            "status": "ok",
            "runs": runs_touched,
            "targets": targets_processed,
        }


class FinalDismissalLifecycleWorker:
    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="final-dismissal-lifecycle",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run_once(self) -> None:
        with self.session_factory() as db:
            try:
                result = FinalDismissalLifecycleService(
                    self.settings,
                    db,
                ).process()
                if result.get("targets"):
                    logger.info(
                        "Автоблокировка увольнений: обработано целей %s",
                        result.get("targets"),
                    )
            except Exception:
                db.rollback()
                logger.exception(
                    "Ошибка автоматической блокировки при увольнении"
                )

    def _run_loop(self) -> None:
        # Первая минута после запуска отводится catch-up импорту 1С.
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
