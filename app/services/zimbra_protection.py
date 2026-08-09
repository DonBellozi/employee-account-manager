from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models_zimbra_observer import ZimbraLifecycleState, ZimbraObserverSettings
from app.models_zimbra_protection import (
    ZimbraProtectedAccount,
    ZimbraProtectionEvent,
    ZimbraProtectionMigration,
)
from app.services.zimbra_observer import (
    NEVER_DISABLE_RE,
    Evaluation,
    HRProtectionSnapshot,
    ObservedZimbraAccount,
    ZimbraObserverService,
    as_utc,
    recommendation_label as base_recommendation_label,
    utcnow,
)


SOURCE_LABELS = {
    "manual": "Вручную",
    "legacy_zimbra_notes": "Импорт из zimbraNotes",
}


@dataclass(frozen=True)
class ManagedObservedZimbraAccount(ObservedZimbraAccount):
    display_name: str = ""


class ManagedZimbraObserverService(ZimbraObserverService):
    """Наблюдатель Zimbra с управляемыми Web-исключениями.

    До первого успешного импорта legacy-маркер never_disable в zimbraNotes
    продолжает работать как страховка. После импорта источником истины становится
    таблица ZimbraProtectedAccount. Изменений в самой Zimbra этот сервис не делает.
    """

    def __init__(self, settings: Settings, db: Session):
        super().__init__(settings, db)
        self._protection_cache: dict[str, ZimbraProtectedAccount] = {}
        self._migration_completed_cache: bool | None = None

    def protection_migration(self) -> ZimbraProtectionMigration | None:
        return self.db.get(ZimbraProtectionMigration, 1)

    def migration_view(self) -> dict[str, object]:
        row = self.protection_migration()
        return {
            "completed": row is not None,
            "completed_at": row.completed_at if row is not None else None,
            "completed_by": row.completed_by if row is not None else "",
            "last_import_at": row.last_import_at if row is not None else None,
            "last_import_by": row.last_import_by if row is not None else "",
        }

    def list_protections(self, limit: int = 1000) -> list[ZimbraProtectedAccount]:
        return list(
            self.db.scalars(
                select(ZimbraProtectedAccount)
                .order_by(
                    case((ZimbraProtectedAccount.is_active.is_(True), 0), else_=1),
                    ZimbraProtectedAccount.display_name,
                    ZimbraProtectedAccount.primary_email,
                )
                .limit(max(1, min(int(limit), 5000)))
            ).all()
        )

    @staticmethod
    def source_label(value: str) -> str:
        return SOURCE_LABELS.get(value, value or "–")


    def _record_event(
        self,
        row: ZimbraProtectedAccount,
        *,
        action: str,
        actor: str,
        reason: str = "",
        created_at: datetime | None = None,
    ) -> None:
        self.db.add(
            ZimbraProtectionEvent(
                protection_id=int(row.id or 0),
                zimbra_id=row.zimbra_id,
                primary_email=row.primary_email,
                display_name=row.display_name,
                action=action,
                actor=actor,
                reason=reason or row.reason,
                created_at=created_at or utcnow(),
            )
        )

    def _migration_completed(self) -> bool:
        if self._migration_completed_cache is None:
            self._migration_completed_cache = self.protection_migration() is not None
        return self._migration_completed_cache

    def _load_protection_cache(self) -> None:
        rows = self.db.scalars(
            select(ZimbraProtectedAccount).where(
                ZimbraProtectedAccount.is_active.is_(True)
            )
        ).all()
        self._protection_cache = {
            row.zimbra_id.strip(): row
            for row in rows
            if row.zimbra_id.strip()
        }
        self._migration_completed_cache = self.protection_migration() is not None

    @classmethod
    def _parse_gaa_verbose(cls, output: str) -> list[ManagedObservedZimbraAccount]:
        rows = super()._parse_gaa_verbose(output)
        display_by_key: dict[str, str] = {}
        current_name = ""
        attrs: dict[str, list[str]] = {}

        def flush() -> None:
            nonlocal current_name, attrs
            if not current_name and not attrs:
                return
            zimbra_id = cls._first_attr(attrs, "zimbraid").strip()
            primary = (
                cls._first_attr(attrs, "mail", "zimbramaildeliveryaddress")
                or current_name
            ).strip().lower()
            key = (zimbra_id or primary).lower()
            if key:
                display_by_key[key] = cls._first_attr(attrs, "displayname").strip()
            current_name = ""
            attrs = {}

        for raw_line in str(output or "").splitlines():
            line = raw_line.rstrip("\r\n")
            if line.startswith("# name "):
                flush()
                current_name = line[7:].strip()
                continue
            if not line.strip():
                continue
            if ":" in line:
                name, value = line.split(":", 1)
                attrs.setdefault(name.strip().lower(), []).append(value.strip())
        flush()

        result: list[ManagedObservedZimbraAccount] = []
        for row in rows:
            key = (row.zimbra_id or row.primary_email).strip().lower()
            result.append(
                ManagedObservedZimbraAccount(
                    zimbra_id=row.zimbra_id,
                    primary_email=row.primary_email,
                    addresses=row.addresses,
                    account_status=row.account_status,
                    last_logon_at=row.last_logon_at,
                    created_at=row.created_at,
                    note=row.note,
                    display_name=display_by_key.get(key, ""),
                )
            )
        return result

    def _refresh_protection_metadata(
        self, accounts: list[ObservedZimbraAccount]
    ) -> None:
        rows = self.db.scalars(select(ZimbraProtectedAccount)).all()
        by_id = {row.zimbra_id: row for row in rows if row.zimbra_id}
        now = utcnow()
        changed = False
        for account in accounts:
            row = by_id.get(account.zimbra_id.strip())
            if row is None:
                continue
            display_name = str(getattr(account, "display_name", "") or "").strip()
            if row.primary_email != account.primary_email:
                row.primary_email = account.primary_email
                changed = True
            if display_name and row.display_name != display_name:
                row.display_name = display_name
                changed = True
            row.last_seen_at = now
            changed = True
        if changed:
            self.db.commit()

    def _fetch_accounts(self) -> list[ObservedZimbraAccount]:
        accounts = super()._fetch_accounts()
        self._refresh_protection_metadata(accounts)
        self._load_protection_cache()
        return accounts

    def _evaluate(
        self,
        account: ObservedZimbraAccount,
        *,
        previous_state: ZimbraLifecycleState | None,
        config: ZimbraObserverSettings,
        hr: HRProtectionSnapshot,
        dismissal_map: dict[str, date],
        now: datetime,
        local_today: date,
    ) -> Evaluation:
        protected = self._protection_cache.get(account.zimbra_id.strip())
        if protected is not None and protected.is_active:
            reason_text = protected.reason.strip() or "Причина не указана"
            return Evaluation(
                recommendation="protected_note",
                reason=(
                    f"Учетная запись защищена в Web. Причина: {reason_text}. "
                    "Закрытие, архивация и удаление не рекомендуются."
                ),
                first_observed_closed_at=(
                    as_utc(previous_state.first_observed_closed_at)
                    if previous_state is not None
                    else None
                ),
            )

        if self._migration_completed() and NEVER_DISABLE_RE.search(account.note or ""):
            # После миграции zimbraNotes больше не является источником истины.
            # Удаляем только legacy-маркер из копии данных, не меняя Zimbra.
            account = replace(
                account,
                note=NEVER_DISABLE_RE.sub(" ", account.note or "").strip(),
            )

        return super()._evaluate(
            account,
            previous_state=previous_state,
            config=config,
            hr=hr,
            dismissal_map=dismissal_map,
            now=now,
            local_today=local_today,
        )

    def import_legacy_never_disable(self, operator: str) -> dict[str, int]:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Проверка Zimbra уже выполняется")
        try:
            accounts = self._fetch_accounts()
            if not accounts:
                raise RuntimeError("Zimbra не вернула ни одной учетной записи")

            existing = {
                row.zimbra_id: row
                for row in self.db.scalars(select(ZimbraProtectedAccount)).all()
                if row.zimbra_id
            }
            now = utcnow()
            found = imported = already_active = inactive_skipped = without_id = 0

            for account in accounts:
                if not NEVER_DISABLE_RE.search(account.note or ""):
                    continue
                found += 1
                zimbra_id = account.zimbra_id.strip()
                if not zimbra_id:
                    without_id += 1
                    continue
                display_name = str(getattr(account, "display_name", "") or "").strip()
                row = existing.get(zimbra_id)
                if row is not None:
                    row.primary_email = account.primary_email
                    if display_name:
                        row.display_name = display_name
                    row.last_seen_at = now
                    if row.is_active:
                        already_active += 1
                    else:
                        # Намеренно не восстанавливаем снятую в Web защиту.
                        inactive_skipped += 1
                    continue

                row = ZimbraProtectedAccount(
                    zimbra_id=zimbra_id,
                    primary_email=account.primary_email,
                    display_name=display_name,
                    source="legacy_zimbra_notes",
                    reason="Импортировано из zimbraNotes: never_disable",
                    is_active=True,
                    activated_by=operator,
                    activated_at=now,
                    last_seen_at=now,
                )
                self.db.add(row)
                self.db.flush()
                self._record_event(
                    row,
                    action="imported",
                    actor=operator,
                    reason=row.reason,
                    created_at=now,
                )
                existing[zimbra_id] = row
                imported += 1

            migration = self.protection_migration()
            if migration is None:
                migration = ZimbraProtectionMigration(
                    id=1,
                    completed_at=now,
                    completed_by=operator,
                    last_import_at=now,
                    last_import_by=operator,
                )
                self.db.add(migration)
            else:
                migration.last_import_at = now
                migration.last_import_by = operator

            self.db.commit()
            self._migration_completed_cache = True
            self._load_protection_cache()
            return {
                "found": found,
                "imported": imported,
                "already_active": already_active,
                "inactive_skipped": inactive_skipped,
                "without_id": without_id,
            }
        except Exception:
            self.db.rollback()
            raise
        finally:
            self._run_lock.release()

    def _find_account(self, email: str) -> ObservedZimbraAccount:
        normalized = str(email or "").strip().lower()
        if not normalized or "@" not in normalized:
            raise ValueError("Укажите корректный адрес почтового ящика")
        accounts = self._fetch_accounts()
        matches = [
            account
            for account in accounts
            if normalized in {item.strip().lower() for item in account.addresses}
        ]
        if not matches:
            raise ValueError("Учетная запись с таким адресом или алиасом не найдена в Zimbra")
        if len(matches) > 1:
            raise ValueError("Адрес соответствует нескольким учетным записям Zimbra")
        if not matches[0].zimbra_id.strip():
            raise ValueError("Zimbra не вернула zimbraId для учетной записи")
        return matches[0]

    def protect_manually(self, email: str, reason: str, operator: str) -> ZimbraProtectedAccount:
        account = self._find_account(email)
        now = utcnow()
        row = self.db.scalar(
            select(ZimbraProtectedAccount).where(
                ZimbraProtectedAccount.zimbra_id == account.zimbra_id.strip()
            )
        )
        display_name = str(getattr(account, "display_name", "") or "").strip()
        clean_reason = str(reason or "").strip() or "Ручное исключение"
        if row is None:
            row = ZimbraProtectedAccount(
                zimbra_id=account.zimbra_id.strip(),
                primary_email=account.primary_email,
                display_name=display_name,
                source="manual",
                reason=clean_reason,
                is_active=True,
                activated_by=operator,
                activated_at=now,
                last_seen_at=now,
            )
            self.db.add(row)
        else:
            row.primary_email = account.primary_email
            if display_name:
                row.display_name = display_name
            row.source = "manual"
            row.reason = clean_reason
            row.is_active = True
            row.activated_by = operator
            row.activated_at = now
            row.deactivated_by = ""
            row.deactivated_at = None
            row.last_seen_at = now
        self.db.flush()
        self._record_event(
            row,
            action="activated",
            actor=operator,
            reason=clean_reason,
            created_at=now,
        )
        self.db.commit()
        self.db.refresh(row)
        self._load_protection_cache()
        return row

    def deactivate(self, protection_id: int, operator: str) -> ZimbraProtectedAccount:
        row = self.db.get(ZimbraProtectedAccount, int(protection_id))
        if row is None:
            raise ValueError("Исключение не найдено")
        if row.is_active:
            row.is_active = False
            row.deactivated_by = operator
            row.deactivated_at = utcnow()
            self.db.flush()
            self._record_event(
                row,
                action="deactivated",
                actor=operator,
                reason=row.reason,
                created_at=row.deactivated_at,
            )
            self.db.commit()
            self.db.refresh(row)
        self._load_protection_cache()
        return row

    def reactivate(self, protection_id: int, operator: str) -> ZimbraProtectedAccount:
        row = self.db.get(ZimbraProtectedAccount, int(protection_id))
        if row is None:
            raise ValueError("Исключение не найдено")

        accounts = self._fetch_accounts()
        account = next(
            (item for item in accounts if item.zimbra_id.strip() == row.zimbra_id),
            None,
        )
        if account is None:
            raise ValueError("Учетная запись с этим zimbraId сейчас не найдена в Zimbra")

        row.primary_email = account.primary_email
        display_name = str(getattr(account, "display_name", "") or "").strip()
        if display_name:
            row.display_name = display_name
        row.source = "manual"
        row.is_active = True
        row.activated_by = operator
        row.activated_at = utcnow()
        row.deactivated_by = ""
        row.deactivated_at = None
        row.last_seen_at = utcnow()
        self.db.flush()
        self._record_event(
            row,
            action="reactivated",
            actor=operator,
            reason=row.reason,
            created_at=row.activated_at,
        )
        self.db.commit()
        self.db.refresh(row)
        self._load_protection_cache()
        return row


def recommendation_label(value: str) -> str:
    if value == "protected_note":
        return "Защищена от закрытия"
    return base_recommendation_label(value)
