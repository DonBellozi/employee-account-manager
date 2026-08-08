from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    BlockingOperation,
    EmailLoginMapping,
    HRSourceRecord,
    OperationStatus,
)
from app.services.ad import ADDirectoryUser, ActiveDirectoryService
from app.services.hr_registry import HRRegistryService
from app.services.itinvent import ITInventEmployeeAssets, ITInventService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BlockingCard:
    record_id: int
    worker_key: str
    source_id: str
    fio: str
    corporate_email: str
    placements: tuple[str, ...]
    effective_login: str
    ad_user: ADDirectoryUser | None
    ad_error: str
    zimbra: ZimbraAccountIdentity | None
    zimbra_error: str
    zimbra_status_label: str
    itinvent: ITInventEmployeeAssets | None
    itinvent_state: str
    itinvent_error: str


@dataclass(frozen=True)
class BlockingResult:
    operation_id: int
    fio: str
    login: str
    corporate_email: str
    status: str
    status_label: str
    ad_disabled: bool
    zimbra_locked: bool
    equipment_count: int
    dry_run: bool
    error_message: str


class BlockingService:
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @classmethod
    def _lock_for(cls, key: str) -> threading.Lock:
        normalized = str(key or "").strip().lower()
        with cls._locks_guard:
            lock = cls._locks.get(normalized)
            if lock is None:
                lock = threading.Lock()
                cls._locks[normalized] = lock
            return lock

    @classmethod
    def _release_lock(cls, key: str, lock: threading.Lock) -> None:
        lock.release()
        normalized = str(key or "").strip().lower()
        with cls._locks_guard:
            current = cls._locks.get(normalized)
            if current is lock and not lock.locked():
                cls._locks.pop(normalized, None)

    def search(self, query: str, limit: int = 100) -> list[dict]:
        text = str(query or "").strip()
        if len(text) < 2:
            return []
        return HRRegistryService(self.settings, self.db).list_rows(
            query=text,
            status="all",
            limit=max(1, min(limit, 200)),
        )

    @staticmethod
    def _placements(record: HRSourceRecord) -> tuple[str, ...]:
        try:
            raw = json.loads(record.placements_json or "[]")
        except json.JSONDecodeError:
            raw = []
        result: list[str] = []
        for value in raw:
            department = str(value.get("department") or "").strip()
            position = str(value.get("position") or "").strip()
            label = " / ".join(part for part in (department, position) if part)
            if label:
                result.append(label)
        return tuple(result)

    def _mapping(self, record: HRSourceRecord) -> EmailLoginMapping | None:
        return self.db.scalar(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key == record.worker_key,
                EmailLoginMapping.source_domain == record.source_id,
            )
        )

    @staticmethod
    def _zimbra_label(identity: ZimbraAccountIdentity | None) -> str:
        if identity is None:
            return "Не найдена"
        labels = {
            "active": "Активна",
            "locked": "Заблокирована",
            "closed": "Закрыта",
            "maintenance": "Обслуживание",
        }
        return labels.get(
            identity.account_status,
            identity.account_status or "Существует",
        )

    def card(self, record_id: int) -> BlockingCard:
        record = self.db.get(HRSourceRecord, record_id)
        if record is None or not record.is_present:
            raise LookupError("Работник не найден в текущем кадровом реестре")

        mapping = self._mapping(record)
        ad_user: ADDirectoryUser | None = None
        z_identity: ZimbraAccountIdentity | None = None
        ad_error = ""
        zimbra_error = ""

        zimbra_service = ZimbraService(self.settings)
        if record.corporate_email.strip() and self.settings.zimbra_check_enabled:
            try:
                if mapping is not None and mapping.zimbra_id.strip():
                    z_identity = zimbra_service.accounts_by_ids(
                        [mapping.zimbra_id]
                    ).get(mapping.zimbra_id)
                if z_identity is None:
                    z_identity = zimbra_service.account_by_address(
                        record.corporate_email
                    )
            except Exception as exc:
                zimbra_error = str(exc)

        candidate_login = ""
        if mapping is not None and mapping.ad_login.strip():
            candidate_login = mapping.ad_login.strip().lower()
        elif z_identity is not None:
            candidate_login = z_identity.login.strip().lower()
        else:
            candidate_login = record.login.strip().lower()

        if self.settings.ad_check_enabled:
            try:
                ad_service = ActiveDirectoryService(self.settings)
                if mapping is not None and mapping.ad_object_guid.strip():
                    ad_user = ad_service.get_user_by_object_guid(
                        mapping.ad_object_guid
                    )
                if ad_user is None and candidate_login:
                    ad_user = ad_service.get_user(candidate_login)
            except Exception as exc:
                ad_error = str(exc)

        effective_login = (
            ad_user.username
            if ad_user is not None
            else candidate_login
        )

        itinvent: ITInventEmployeeAssets | None = None
        itinvent_state = "not_configured"
        itinvent_error = ""
        itinvent_service = ITInventService(self.settings)
        if self.settings.itinvent_enabled and itinvent_service.configured:
            if not effective_login:
                itinvent_state = "no_login"
            else:
                try:
                    itinvent = itinvent_service.equipment_for_login(
                        effective_login
                    )
                    itinvent_state = (
                        "found"
                        if itinvent.owner_found
                        else "owner_not_found"
                    )
                except Exception as exc:
                    itinvent_state = "error"
                    itinvent_error = str(exc)

        return BlockingCard(
            record_id=record.id,
            worker_key=record.worker_key,
            source_id=record.source_id,
            fio=record.fio,
            corporate_email=record.corporate_email,
            placements=self._placements(record),
            effective_login=effective_login,
            ad_user=ad_user,
            ad_error=ad_error,
            zimbra=z_identity,
            zimbra_error=zimbra_error,
            zimbra_status_label=self._zimbra_label(z_identity),
            itinvent=itinvent,
            itinvent_state=itinvent_state,
            itinvent_error=itinvent_error,
        )

    def block(self, record_id: int, operator: str) -> BlockingResult:
        first = self.card(record_id)
        lock_key = first.worker_key or first.effective_login or str(record_id)
        lock = self._lock_for(lock_key)
        if not lock.acquire(blocking=False):
            raise RuntimeError(
                "Блокировка этого работника уже выполняется. "
                "Дождитесь завершения текущей операции."
            )

        try:
            card = self.card(record_id)
            equipment = (
                list(card.itinvent.equipment)
                if card.itinvent is not None
                else []
            )
            operation = BlockingOperation(
                worker_key=card.worker_key,
                source_id=card.source_id,
                source_record_id=card.record_id,
                operator_username=operator,
                full_name=card.fio,
                login=card.effective_login,
                corporate_email=card.corporate_email,
                status=OperationStatus.RUNNING,
                dry_run=self.settings.dry_run,
                itinvent_checked=(
                    card.itinvent_state
                    in {"found", "owner_not_found"}
                ),
                itinvent_owner_name=(
                    card.itinvent.owner_display_name
                    if card.itinvent is not None
                    else ""
                ),
                equipment_count=len(equipment),
                equipment_snapshot_json=json.dumps(
                    [
                        {
                            "type": item.equipment_type,
                            "serial_number": item.serial_number,
                            "inventory_number": item.inventory_number,
                            "accounting_inventory_number": (
                                item.accounting_inventory_number
                            ),
                        }
                        for item in equipment
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            self.db.add(operation)
            self.db.commit()
            self.db.refresh(operation)

            errors: list[str] = []
            completed_actions = 0

            if card.ad_user is None:
                errors.append(
                    "AD: учетная запись не найдена"
                    + (f" ({card.ad_error})" if card.ad_error else "")
                )
            else:
                try:
                    ActiveDirectoryService(self.settings).disable_user(
                        card.ad_user.username
                    )
                    operation.ad_disabled = True
                    completed_actions += 1
                except Exception as exc:
                    errors.append(f"AD: {exc}")

            if card.zimbra is None:
                errors.append(
                    "Zimbra: учетная запись не найдена"
                    + (
                        f" ({card.zimbra_error})"
                        if card.zimbra_error
                        else ""
                    )
                )
            else:
                try:
                    ZimbraService(self.settings).lock_account(
                        card.zimbra.primary_email
                    )
                    operation.zimbra_locked = True
                    completed_actions += 1
                except Exception as exc:
                    errors.append(f"Zimbra: {exc}")

            if completed_actions == 0:
                operation.status = OperationStatus.FAILED
            elif errors:
                operation.status = OperationStatus.PARTIAL
            else:
                operation.status = OperationStatus.SUCCESS

            operation.error_message = "\n".join(errors)[:4000]
            operation.completed_at = utcnow()

            audit_result = (
                "success"
                if operation.status == OperationStatus.SUCCESS
                else "partial"
                if operation.status == OperationStatus.PARTIAL
                else "failed"
            )
            self.db.add(
                AuditLog(
                    actor=operator,
                    action="block_accounts",
                    target=card.effective_login or card.corporate_email,
                    result=audit_result,
                    details=(
                        f"worker_key={card.worker_key}; "
                        f"AD={operation.ad_disabled}; "
                        f"Zimbra={operation.zimbra_locked}; "
                        f"ITInvent={operation.equipment_count}; "
                        f"dry_run={operation.dry_run}"
                    )[:1000],
                )
            )
            self.db.commit()

            status_key = operation.status.value
            labels = {
                "success": "Успешно",
                "partial": "Частично выполнено",
                "failed": "Ошибка",
            }
            if operation.dry_run:
                status_label = "DRY RUN"
            else:
                status_label = labels.get(status_key, status_key)

            return BlockingResult(
                operation_id=operation.id,
                fio=operation.full_name,
                login=operation.login,
                corporate_email=operation.corporate_email,
                status=status_key,
                status_label=status_label,
                ad_disabled=operation.ad_disabled,
                zimbra_locked=operation.zimbra_locked,
                equipment_count=operation.equipment_count,
                dry_run=operation.dry_run,
                error_message=operation.error_message,
            )
        except Exception:
            self.db.rollback()
            raise
        finally:
            self._release_lock(lock_key, lock)
