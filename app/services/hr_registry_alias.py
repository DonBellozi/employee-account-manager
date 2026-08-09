from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.services.hr_registry_manual_mapping import HRRegistryManualMappingService
from app.services.zimbra import ZimbraAccountIdentity, ZimbraService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


class HRRegistryAliasService:
    """Предлагает и создает alias текущей организации на соседнем Zimbra-ящике."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def _active_records(self) -> list[HRSourceRecord]:
        return list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.is_present.is_(True)
                )
            ).all()
        )

    def _mappings(
        self,
        worker_keys: list[str],
    ) -> list[EmailLoginMapping]:
        if not worker_keys:
            return []
        return list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key.in_(worker_keys)
                )
            ).all()
        )

    @staticmethod
    def _mail_domains(settings: Settings) -> set[str]:
        return {
            normalize(domain).lstrip("@")
            for domain in getattr(settings, "zimbra_domains", ()) or ()
            if normalize(domain).lstrip("@")
        }

    @staticmethod
    def _source_name(record: HRSourceRecord) -> str:
        return str(record.source_name or record.source_id or "").strip()

    @staticmethod
    def _candidate_addresses(
        record: HRSourceRecord,
        siblings: list[HRSourceRecord],
        mapping_by_pair: dict[tuple[str, str], EmailLoginMapping],
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(
            *,
            email: str,
            source_id: str,
            source_name: str,
        ) -> None:
            normalized = normalize(email)
            if not normalized or "@" not in normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(
                {
                    "email": normalized,
                    "source_id": normalize(source_id),
                    "source_name": source_name or source_id,
                }
            )

        for sibling in siblings:
            if sibling.id == record.id:
                continue
            if sibling.worker_key != record.worker_key:
                continue
            if not sibling.is_present:
                continue

            add(
                email=sibling.corporate_email,
                source_id=sibling.source_id,
                source_name=HRRegistryAliasService._source_name(sibling),
            )

            mapping = mapping_by_pair.get(
                (
                    sibling.worker_key,
                    normalize(sibling.source_id),
                )
            )
            if mapping is not None:
                add(
                    email=mapping.source_email,
                    source_id=sibling.source_id,
                    source_name=HRRegistryAliasService._source_name(sibling),
                )
                add(
                    email=mapping.zimbra_email,
                    source_id=sibling.source_id,
                    source_name=HRRegistryAliasService._source_name(sibling),
                )

        return candidates

    def suggestions(
        self,
        *,
        active_records: list[HRSourceRecord] | None = None,
        mappings: list[EmailLoginMapping] | None = None,
    ) -> dict[int, dict]:
        """Быстрые локальные подсказки для кадрового реестра.

        Здесь намеренно НЕТ обращений к Zimbra. Страница кадрового реестра
        должна строиться только из локальной БД. Точная проверка физического
        ящика и занятости алиаса выполняется в plan() только после клика.
        """
        records = (
            active_records
            if active_records is not None
            else self._active_records()
        )
        worker_keys = list({record.worker_key for record in records})
        mapping_rows = (
            mappings
            if mappings is not None
            else self._mappings(worker_keys)
        )

        by_worker: dict[str, list[HRSourceRecord]] = defaultdict(list)
        for record in records:
            by_worker[record.worker_key].append(record)

        mapping_by_pair = {
            (
                mapping.worker_key,
                normalize(mapping.source_domain),
            ): mapping
            for mapping in mapping_rows
        }

        result: dict[int, dict] = {}

        for record in records:
            if normalize(record.corporate_email):
                continue

            candidates = self._candidate_addresses(
                record,
                by_worker.get(record.worker_key, []),
                mapping_by_pair,
            )
            if not candidates:
                continue

            unique_localparts = {
                candidate["email"].split("@", 1)[0]
                for candidate in candidates
                if "@" in candidate["email"]
            }
            source_domain = normalize(record.source_id)
            proposed_alias = ""
            can_open = False
            note = "Соседняя почта найдена."

            if len(unique_localparts) == 1 and source_domain:
                proposed_alias = (
                    f"{next(iter(unique_localparts))}@{source_domain}"
                )
                can_open = True
                note = (
                    "Соседняя почта найдена. "
                    "Zimbra будет проверена при открытии алиаса."
                )
            elif len(unique_localparts) > 1:
                note = (
                    "В соседних организациях найдены разные логины почты. "
                    "Алиас автоматически не предлагается."
                )

            result[record.id] = {
                "record_id": record.id,
                "worker_key": record.worker_key,
                "fio": record.fio,
                "source_id": source_domain,
                "source_name": self._source_name(record),
                "candidates": candidates,
                "sibling_email": candidates[0]["email"],
                "sibling_source_id": candidates[0]["source_id"],
                "sibling_source_name": candidates[0]["source_name"],
                "mailbox_found": False,
                "mailbox_primary": "",
                "mailbox_zimbra_id": "",
                "proposed_alias": proposed_alias,
                "alias_exists": False,
                "alias_conflict": False,
                "can_create": False,
                "can_bind": False,
                "can_open": can_open,
                "note": note,
            }

        return result

    def plan(self, record_id: int) -> dict:
        """Точная Zimbra-проверка только для одного выбранного работника."""
        record = self.db.get(HRSourceRecord, int(record_id))
        if record is None or not record.is_present:
            raise LookupError("Работник не найден в текущем кадровом реестре")
        if normalize(record.corporate_email):
            raise ValueError(
                "В кадровой выгрузке уже указан корпоративный e-mail"
            )

        local_item = self.suggestions().get(record.id)
        if local_item is None:
            raise ValueError(
                "У работника не найдена почта в соседней организации"
            )

        if not bool(getattr(self.settings, "zimbra_check_enabled", False)):
            return {
                **local_item,
                "can_open": False,
                "note": "Проверка Zimbra отключена.",
            }

        if str(
            getattr(self.settings, "zimbra_backend", "")
        ).strip().lower() == "disabled":
            return {
                **local_item,
                "can_open": False,
                "note": "Zimbra backend отключен.",
            }

        domains = self._mail_domains(self.settings)
        source_domain = local_item["source_id"]
        if source_domain not in domains:
            return {
                **local_item,
                "can_open": False,
                "note": (
                    f"Домен {source_domain} не входит "
                    "в настроенные домены Zimbra."
                ),
            }

        zimbra = ZimbraService(self.settings)
        candidate_addresses = [
            candidate["email"]
            for candidate in local_item["candidates"]
        ]

        try:
            neighbor_accounts = zimbra.accounts_by_addresses(
                candidate_addresses
            )
        except Exception as exc:
            return {
                **local_item,
                "can_open": False,
                "note": f"Ошибка проверки Zimbra: {exc}",
            }

        identities: dict[str, ZimbraAccountIdentity] = {}
        source_for_identity: dict[str, dict[str, str]] = {}

        for candidate in local_item["candidates"]:
            identity = neighbor_accounts.get(candidate["email"])
            if identity is None:
                continue
            identities[identity.zimbra_id] = identity
            source_for_identity.setdefault(
                identity.zimbra_id,
                candidate,
            )

        if not identities:
            return {
                **local_item,
                "can_open": False,
                "note": (
                    "Соседняя почта есть в кадрах, "
                    "но ящик Zimbra не найден."
                ),
            }

        if len(identities) > 1:
            return {
                **local_item,
                "can_open": False,
                "note": (
                    "В соседних организациях найдены разные "
                    "физические ящики Zimbra. Алиас автоматически "
                    "не предлагается."
                ),
            }

        identity = next(iter(identities.values()))
        source = source_for_identity[identity.zimbra_id]
        proposed_alias = f"{identity.login}@{source_domain}"

        try:
            existing = zimbra.account_by_address(proposed_alias)
        except Exception as exc:
            return {
                **local_item,
                "sibling_email": source["email"],
                "sibling_source_id": source["source_id"],
                "sibling_source_name": source["source_name"],
                "mailbox_found": True,
                "mailbox_primary": identity.primary_email,
                "mailbox_zimbra_id": identity.zimbra_id,
                "proposed_alias": proposed_alias,
                "can_open": False,
                "note": (
                    "Не удалось проверить предлагаемый алиас: "
                    f"{exc}"
                ),
            }

        item = {
            **local_item,
            "sibling_email": source["email"],
            "sibling_source_id": source["source_id"],
            "sibling_source_name": source["source_name"],
            "mailbox_found": True,
            "mailbox_primary": identity.primary_email,
            "mailbox_zimbra_id": identity.zimbra_id,
            "proposed_alias": proposed_alias,
            "alias_exists": False,
            "alias_conflict": False,
            "can_create": False,
            "can_bind": False,
            "can_open": True,
            "note": "",
        }

        if existing is None:
            item["can_create"] = True
            item["note"] = (
                f"Можно добавить алиас {proposed_alias} "
                f"к ящику {identity.primary_email}."
            )
            return item

        if existing.zimbra_id == identity.zimbra_id:
            item["alias_exists"] = True
            item["can_bind"] = True
            item["note"] = (
                f"Алиас {proposed_alias} уже принадлежит этому "
                "ящику. Можно привязать его к кадровой записи."
            )
            return item

        item["alias_conflict"] = True
        item["can_open"] = False
        item["note"] = (
            f"Адрес {proposed_alias} уже занят другим ящиком Zimbra."
        )
        return item

    def create_or_bind(
        self,
        *,
        record_id: int,
        actor: str,
    ) -> dict:
        item = self.plan(record_id)

        if item["alias_conflict"]:
            raise ValueError(item["note"])
        if not item["can_create"] and not item["can_bind"]:
            raise ValueError(item["note"] or "Создание алиаса недоступно")

        if getattr(self.settings, "dry_run", False):
            return {
                **item,
                "status": "dry_run",
                "dry_run": True,
                "created": False,
                "bound": False,
            }

        created = False
        zimbra = ZimbraService(self.settings)

        if item["can_create"]:
            # Используем штатную команду addAccountAlias (`aaa`) через уже
            # существующий безопасный SSH/zmprov слой ZimbraService.
            zimbra._run_zmprov_direct(
                [
                    "aaa",
                    item["mailbox_primary"],
                    item["proposed_alias"],
                ]
            )
            created = True

            verified = zimbra.account_by_address(item["proposed_alias"])
            if (
                verified is None
                or verified.zimbra_id != item["mailbox_zimbra_id"]
            ):
                raise RuntimeError(
                    "Алиас создан командой Zimbra, но контрольная проверка "
                    "не подтвердила его принадлежность ожидаемому ящику"
                )

        mapping_result = HRRegistryManualMappingService(
            self.settings,
            self.db,
        ).save_identifier(
            record_id=record_id,
            identifier=item["proposed_alias"],
            actor=actor,
        )

        self.db.add(
            AuditLog(
                actor=actor,
                action=(
                    "hr_registry_zimbra_alias_create"
                    if created
                    else "hr_registry_zimbra_alias_bind"
                ),
                target=f"{item['source_id']}:{item['worker_key']}",
                result="success",
                details=json.dumps(
                    {
                        "record_id": record_id,
                        "fio": item["fio"],
                        "source_id": item["source_id"],
                        "neighbor_email": item["sibling_email"],
                        "mailbox_primary": item["mailbox_primary"],
                        "zimbra_id": item["mailbox_zimbra_id"],
                        "alias": item["proposed_alias"],
                        "created": created,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                created_at=utcnow(),
            )
        )
        self.db.commit()

        return {
            **item,
            "status": "created" if created else "bound",
            "dry_run": False,
            "created": created,
            "bound": True,
            "mapping": mapping_result,
        }
