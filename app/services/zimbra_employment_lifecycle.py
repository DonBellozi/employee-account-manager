from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EmailLoginMapping, HRSourceRecord, OneCImportRun
from app.models_dismissals import DismissalDeferral
from app.models_onec_sources import HREmploymentState
from app.models_zimbra_lifecycle import (
    ZimbraEmploymentAction,
    ZimbraLifecycleSettings,
)
from app.services.blocking_window import is_block_window_open
from app.services.onec_freshness import OneCSourceFreshnessService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


logger = logging.getLogger(__name__)
POLL_SECONDS = 60
ACTIVE_STATUSES = {"active", "scheduled"}
OPEN_STATUSES = {"pending", "awaiting_permission", "intervention", "failed"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


def address_domain(value: str) -> str:
    address = normalize(value)
    return address.rsplit("@", 1)[1] if address.count("@") == 1 else ""


@dataclass(frozen=True)
class ZimbraEmploymentSpec:
    plan_key: str
    worker_key: str
    fio: str
    zimbra_id: str
    action: str
    source_id: str
    target_address: str
    replacement_address: str
    dismissal_date: date
    effective_action_date: date
    details: str


class ZimbraEmploymentLifecycleService:
    """Интерпретирует кадровые состояния отдельно для каждого zimbraId."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @property
    def local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.app_timezone))

    def _ready(self) -> bool:
        importing = bool(
            self.db.scalar(
                select(OneCImportRun.id)
                .where(OneCImportRun.status == "running")
                .limit(1)
            )
        )
        if importing:
            return False
        return OneCSourceFreshnessService(
            self.settings, self.db
        ).all_control_exports_ready(expected_date=self.local_now.date())

    def _deferrals(self) -> dict[tuple[str, date], date]:
        return {
            (row.worker_key, row.dismissal_date): row.deferred_until
            for row in self.db.scalars(select(DismissalDeferral)).all()
        }

    def _effective_date(
        self,
        state: HREmploymentState,
        deferrals: dict[tuple[str, date], date],
    ) -> date:
        dismissal_date = state.dismissal_date or self.local_now.date()
        return max(
            dismissal_date,
            deferrals.get((state.worker_key, dismissal_date), dismissal_date),
        )

    def _due(
        self,
        state: HREmploymentState,
        deferrals: dict[tuple[str, date], date],
    ) -> bool:
        if state.status in ACTIVE_STATUSES:
            return False
        effective = self._effective_date(state, deferrals)
        today = self.local_now.date()
        if effective < today:
            return True
        if effective > today:
            return False
        if state.status_reason == "absent_from_export":
            return True
        dismissal_date = state.dismissal_date
        if dismissal_date is not None and dismissal_date < today:
            return True
        return is_block_window_open(self.local_now)

    @staticmethod
    def _key(
        worker_key: str,
        zimbra_id: str,
        action: str,
        target: str,
        dismissal_date: date,
    ) -> str:
        raw = "|".join(
            [worker_key, zimbra_id, action, target, dismissal_date.isoformat()]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _spec(
        self,
        *,
        worker_key: str,
        fio: str,
        identity: ZimbraAccountIdentity,
        action: str,
        state: HREmploymentState,
        target: str,
        replacement: str = "",
        deferrals: dict[tuple[str, date], date],
        details: str,
    ) -> ZimbraEmploymentSpec:
        dismissal_date = state.dismissal_date or self.local_now.date()
        return ZimbraEmploymentSpec(
            plan_key=self._key(
                worker_key,
                identity.zimbra_id,
                action,
                target,
                dismissal_date,
            ),
            worker_key=worker_key,
            fio=fio,
            zimbra_id=identity.zimbra_id,
            action=action,
            source_id=normalize(state.source_id),
            target_address=normalize(target),
            replacement_address=normalize(replacement),
            dismissal_date=dismissal_date,
            effective_action_date=self._effective_date(state, deferrals),
            details=details,
        )

    def _specs_for_mailbox(
        self,
        *,
        worker_key: str,
        fio: str,
        identity: ZimbraAccountIdentity,
        states: dict[str, HREmploymentState],
        deferrals: dict[tuple[str, date], date],
    ) -> list[ZimbraEmploymentSpec]:
        addresses = tuple(dict.fromkeys(normalize(v) for v in identity.addresses if v))
        primary = normalize(identity.primary_email)
        if primary and primary not in addresses:
            addresses = (primary, *addresses)

        due_by_address: dict[str, HREmploymentState] = {}
        active_addresses: list[str] = []
        unknown_addresses: list[str] = []
        for address in addresses:
            state = states.get(address_domain(address))
            if state is None:
                unknown_addresses.append(address)
            elif self._due(state, deferrals):
                due_by_address[address] = state
            else:
                active_addresses.append(address)

        if not due_by_address:
            return []

        primary_state = due_by_address.get(primary)
        if primary_state is not None:
            replacement = next(
                (address for address in active_addresses if address != primary),
                "",
            )
            if replacement:
                return [
                    self._spec(
                        worker_key=worker_key,
                        fio=fio,
                        identity=identity,
                        action="transition",
                        state=primary_state,
                        target=primary,
                        replacement=replacement,
                        deferrals=deferrals,
                        details=(
                            "Основной адрес относится к завершенной занятости, "
                            "но alias того же физического ящика остается активным. "
                            "Требуется backup и решение оператора о смене primary."
                        ),
                    )
                ]
            if unknown_addresses:
                return [
                    self._spec(
                        worker_key=worker_key,
                        fio=fio,
                        identity=identity,
                        action="manual_review",
                        state=primary_state,
                        target=primary,
                        deferrals=deferrals,
                        details=(
                            "Основной адрес уволен, но у физического ящика есть "
                            "адреса без однозначной кадровой организации."
                        ),
                    )
                ]
            return [
                self._spec(
                    worker_key=worker_key,
                    fio=fio,
                    identity=identity,
                    action="close",
                    state=primary_state,
                    target=primary,
                    deferrals=deferrals,
                    details="У физического ящика не осталось действующих адресов организаций.",
                )
            ]

        result: list[ZimbraEmploymentSpec] = []
        for address, state in sorted(due_by_address.items()):
            result.append(
                self._spec(
                    worker_key=worker_key,
                    fio=fio,
                    identity=identity,
                    action="remove_alias",
                    state=state,
                    target=address,
                    deferrals=deferrals,
                    details=(
                        "Удаляется только адрес завершенной организации; "
                        "физический ящик и остальные адреса сохраняются."
                    ),
                )
            )
        return result

    def _current_specs(self) -> list[ZimbraEmploymentSpec]:
        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.zimbra_id != ""
                )
            ).all()
        )
        if not mappings:
            return []

        zimbra_ids = sorted({normalize(row.zimbra_id) for row in mappings if row.zimbra_id})
        identities = ZimbraService(self.settings).accounts_by_ids(zimbra_ids)
        worker_keys = sorted({row.worker_key for row in mappings})
        states = list(
            self.db.scalars(
                select(HREmploymentState).where(
                    HREmploymentState.worker_key.in_(worker_keys)
                )
            ).all()
        )
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key.in_(worker_keys)
                )
            ).all()
        )
        states_by_worker: dict[str, dict[str, HREmploymentState]] = {}
        for state in states:
            states_by_worker.setdefault(state.worker_key, {})[
                normalize(state.source_id)
            ] = state
        fio_by_worker: dict[str, str] = {}
        for row in [*states, *records]:
            if row.worker_key not in fio_by_worker and str(row.fio or "").strip():
                fio_by_worker[row.worker_key] = str(row.fio).strip()

        groups: dict[tuple[str, str], list[EmailLoginMapping]] = {}
        for mapping in mappings:
            groups.setdefault(
                (mapping.worker_key, normalize(mapping.zimbra_id)), []
            ).append(mapping)

        deferrals = self._deferrals()
        result: list[ZimbraEmploymentSpec] = []
        for (worker_key, zimbra_id), _ in groups.items():
            identity = identities.get(zimbra_id)
            if identity is None:
                continue
            result.extend(
                self._specs_for_mailbox(
                    worker_key=worker_key,
                    fio=fio_by_worker.get(worker_key, ""),
                    identity=identity,
                    states=states_by_worker.get(worker_key, {}),
                    deferrals=deferrals,
                )
            )
        return result

    def _ensure_action(self, spec: ZimbraEmploymentSpec) -> ZimbraEmploymentAction:
        row = self.db.scalar(
            select(ZimbraEmploymentAction).where(
                ZimbraEmploymentAction.plan_key == spec.plan_key
            )
        )
        if row is None:
            row = ZimbraEmploymentAction(plan_key=spec.plan_key)
            self.db.add(row)
        row.worker_key = spec.worker_key
        row.fio = spec.fio
        row.zimbra_id = spec.zimbra_id
        row.action = spec.action
        row.source_id = spec.source_id
        row.target_address = spec.target_address
        row.replacement_address = spec.replacement_address
        row.dismissal_date = spec.dismissal_date
        row.effective_action_date = spec.effective_action_date
        row.details = spec.details
        row.updated_at = utcnow()
        if row.status == "cancelled":
            row.status = "pending"
            row.cancelled_at = None
        return row

    def _cancel_stale(self, current_keys: set[str]) -> None:
        rows = list(
            self.db.scalars(
                select(ZimbraEmploymentAction).where(
                    ZimbraEmploymentAction.status.in_(OPEN_STATUSES)
                )
            ).all()
        )
        for row in rows:
            if row.plan_key in current_keys:
                continue
            row.status = "cancelled"
            row.cancelled_at = utcnow()
            row.last_error = "Кадровое состояние или состав адресов Zimbra изменились"
            row.updated_at = utcnow()

    def _settings(self) -> ZimbraLifecycleSettings:
        row = self.db.get(ZimbraLifecycleSettings, 1)
        if row is None:
            row = ZimbraLifecycleSettings(id=1)
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    def _execute(self, row: ZimbraEmploymentAction) -> bool:
        current = {spec.plan_key: spec for spec in self._current_specs()}
        if row.plan_key not in current or not self._ready():
            row.status = "cancelled"
            row.cancelled_at = utcnow()
            row.last_error = "Повторная HR-проверка отменила действие"
            return False

        config = self._settings()
        if row.action in {"transition", "manual_review"}:
            row.status = "intervention"
            return False
        if row.action == "close" and not config.allow_employment_close:
            row.status = "awaiting_permission"
            return False
        if row.action == "remove_alias" and not config.allow_alias_remove:
            row.status = "awaiting_permission"
            return False

        service = ZimbraService(self.settings)
        identity = service.accounts_by_ids([row.zimbra_id]).get(row.zimbra_id)
        row.attempts = int(row.attempts or 0) + 1
        row.last_attempt_at = utcnow()
        if identity is None:
            row.status = "failed"
            row.last_error = "Физический ящик Zimbra не найден перед действием"
            return False

        try:
            if row.action == "close":
                if normalize(identity.account_status) != "closed":
                    service.close_account(identity.primary_email)
                verified = service.accounts_by_ids([row.zimbra_id]).get(row.zimbra_id)
                if verified is None or normalize(verified.account_status) != "closed":
                    raise RuntimeError("Статус closed не подтвержден после команды")
            elif row.action == "remove_alias":
                if row.target_address in {normalize(v) for v in identity.addresses}:
                    service.remove_alias(identity.primary_email, row.target_address)
                verified = service.accounts_by_ids([row.zimbra_id]).get(row.zimbra_id)
                if verified is not None and row.target_address in {
                    normalize(v) for v in verified.addresses
                }:
                    raise RuntimeError("Удаление alias не подтверждено после команды")
            else:
                raise RuntimeError(f"Неизвестное действие Zimbra: {row.action}")
            row.status = "completed"
            row.completed_at = utcnow()
            row.last_error = ""
            return True
        except Exception as exc:
            row.status = "failed"
            row.last_error = str(exc)[:4000]
            return False

    def process(self) -> dict[str, int | str]:
        if not self._ready():
            return {"status": "sources_not_ready", "planned": 0, "completed": 0}
        specs = self._current_specs()
        rows = [self._ensure_action(spec) for spec in specs]
        self._cancel_stale({spec.plan_key for spec in specs})
        self.db.commit()

        completed = 0
        for row in rows:
            if row.status == "completed":
                continue
            if self._execute(row):
                completed += 1
            self.db.commit()
        return {"status": "ok", "planned": len(specs), "completed": completed}

    def open_actions(self, limit: int = 100) -> list[ZimbraEmploymentAction]:
        return list(
            self.db.scalars(
                select(ZimbraEmploymentAction)
                .where(ZimbraEmploymentAction.status.in_(OPEN_STATUSES))
                .order_by(
                    ZimbraEmploymentAction.updated_at.desc(),
                    ZimbraEmploymentAction.id.desc(),
                )
                .limit(max(1, min(limit, 500)))
            ).all()
        )

    def recent_actions(self, limit: int = 100) -> list[ZimbraEmploymentAction]:
        return list(
            self.db.scalars(
                select(ZimbraEmploymentAction)
                .order_by(
                    ZimbraEmploymentAction.updated_at.desc(),
                    ZimbraEmploymentAction.id.desc(),
                )
                .limit(max(1, min(limit, 500)))
            ).all()
        )


class ZimbraEmploymentLifecycleWorker:
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
            name="zimbra-employment-lifecycle",
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
                result = ZimbraEmploymentLifecycleService(
                    self.settings, db
                ).process()
                if result.get("completed"):
                    logger.info(
                        "Кадровый Zimbra lifecycle: выполнено %s",
                        result["completed"],
                    )
            except Exception:
                db.rollback()
                logger.exception("Ошибка кадрового Zimbra lifecycle")

    def _run_loop(self) -> None:
        while not self._stop_event.wait(POLL_SECONDS):
            self._run_once()
