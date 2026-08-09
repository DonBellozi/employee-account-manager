from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import OneCAdditionalSource
from app.services.ad import ActiveDirectoryService
from app.services.hr_registry import HRRegistryService


SUMMARY_KEYS = (
    "total",
    "ok",
    "checked",
    "issues",
    "errors",
    "not_checked",
    "ad_missing",
    "ad_disabled",
    "zimbra_missing",
    "no_email",
    "mapping_count",
)


class MultiSourceHRRegistryViewService:
    """Объединяет кадровые записи всех организаций только для просмотра."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def _configured_sources(self) -> dict[str, str]:
        result: dict[str, str] = {}
        try:
            rows = self.db.scalars(
                select(OneCAdditionalSource).order_by(
                    OneCAdditionalSource.is_primary.desc(),
                    OneCAdditionalSource.name,
                    OneCAdditionalSource.mail_domain,
                )
            ).all()
        except Exception:
            rows = []

        for row in rows:
            domain = str(row.mail_domain or "").strip().lower()
            if domain:
                result[domain] = str(row.name or domain).strip() or domain

        active_records = self.db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.is_present.is_(True)
            )
        ).all()
        for record in active_records:
            domain = str(record.source_id or "").strip().lower()
            if not domain or domain == "org_com":
                continue
            result.setdefault(
                domain,
                str(record.source_name or domain).strip() or domain,
            )
        return result

    def source_options(self) -> list[dict[str, str]]:
        return [
            {"id": domain, "name": name}
            for domain, name in self._configured_sources().items()
        ]

    def _source_ids_with_records(self) -> list[str]:
        values = {
            str(value or "").strip().lower()
            for value in self.db.scalars(
                select(HRSourceRecord.source_id).where(
                    HRSourceRecord.is_present.is_(True)
                )
            ).all()
            if str(value or "").strip().lower() not in {"", "org_com"}
        }
        configured = self._configured_sources()
        return sorted(
            values,
            key=lambda domain: (
                configured.get(domain, domain).casefold(),
                domain,
            ),
        )

    def _settings_for(self, source_id: str):
        if hasattr(self.settings, "model_copy"):
            settings = self.settings.model_copy(deep=True)
        else:
            import copy
            settings = copy.deepcopy(self.settings)

        settings.onec_source_domain = source_id
        domains = [
            str(value or "").strip().lower()
            for value in getattr(settings, "zimbra_domains", [])
            if str(value or "").strip()
        ]
        if source_id not in domains:
            domains.append(source_id)
        settings.zimbra_domains = domains
        return settings

    def _service_for(self, source_id: str) -> HRRegistryService:
        return HRRegistryService(
            self._settings_for(source_id),
            self.db,
        )

    @staticmethod
    def _recalculate_status(record: HRSourceRecord) -> None:
        if (
            record.ad_status == "error"
            or record.zimbra_status == "error"
        ):
            record.reconciliation_status = "error"
        elif (
            record.ad_status in {"missing", "disabled", "no_login"}
            or record.zimbra_status
            in {"missing", "no_email", "address_mismatch"}
        ):
            record.reconciliation_status = "issue"
        elif (
            record.ad_status == "not_checked"
            or record.zimbra_status == "not_checked"
        ):
            record.reconciliation_status = "not_checked"
        else:
            record.reconciliation_status = "ok"

    def _shared_ad_hints(
        self,
        source_id: str,
        worker_keys: list[str],
    ) -> dict[str, dict[str, str]]:
        if not worker_keys:
            return {}

        mappings = self.db.scalars(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key.in_(worker_keys),
                EmailLoginMapping.source_domain != source_id,
            )
        ).all()
        sibling_records = self.db.scalars(
            select(HRSourceRecord).where(
                HRSourceRecord.worker_key.in_(worker_keys),
                HRSourceRecord.source_id != source_id,
            )
        ).all()

        mapped_guids: dict[str, set[str]] = defaultdict(set)
        mapped_logins: dict[str, set[str]] = defaultdict(set)
        active_logins: dict[str, set[str]] = defaultdict(set)
        any_logins: dict[str, set[str]] = defaultdict(set)

        for mapping in mappings:
            guid = str(mapping.ad_object_guid or "").strip().strip("{}").lower()
            login = str(mapping.ad_login or "").strip().lower()
            if guid:
                mapped_guids[mapping.worker_key].add(guid)
            if login:
                mapped_logins[mapping.worker_key].add(login)

        for record in sibling_records:
            login = str(record.login or "").strip().lower()
            if not login:
                continue
            any_logins[record.worker_key].add(login)
            if record.is_present:
                active_logins[record.worker_key].add(login)

        result: dict[str, dict[str, str]] = {}
        for worker_key in worker_keys:
            guids = mapped_guids.get(worker_key, set())
            mapped = mapped_logins.get(worker_key, set())
            active = active_logins.get(worker_key, set())
            any_known = any_logins.get(worker_key, set())

            # Explicit objectGUID is the strongest cross-organization link.
            # Conflicting values are never guessed.
            if len(guids) == 1:
                result[worker_key] = {
                    "guid": next(iter(guids)),
                    "login": next(iter(mapped)) if len(mapped) == 1 else "",
                    "source": "mapping",
                }
                continue
            if len(mapped) == 1:
                result[worker_key] = {
                    "guid": "",
                    "login": next(iter(mapped)),
                    "source": "mapping",
                }
                continue
            if len(active) == 1:
                result[worker_key] = {
                    "guid": "",
                    "login": next(iter(active)),
                    "source": "active_hr",
                }
                continue
            if len(any_known) == 1:
                result[worker_key] = {
                    "guid": "",
                    "login": next(iter(any_known)),
                    "source": "hr",
                }

        return result

    def _resolve_shared_ad_for_source(
        self,
        source_id: str,
    ) -> int:
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.source_id == source_id,
                    HRSourceRecord.is_present.is_(True),
                    HRSourceRecord.ad_status.in_(("no_login", "missing")),
                )
            ).all()
        )
        if not records or not self.settings.ad_check_enabled:
            return 0

        hints = self._shared_ad_hints(
            source_id,
            [record.worker_key for record in records],
        )
        if not hints:
            return 0

        ad = ActiveDirectoryService(self.settings)
        guid_values = sorted(
            {
                hint["guid"]
                for hint in hints.values()
                if hint.get("guid")
            }
        )
        login_values = sorted(
            {
                hint["login"]
                for hint in hints.values()
                if hint.get("login")
            }
        )

        by_guid = {}
        by_login = {}
        try:
            if guid_values:
                by_guid = ad.users_by_object_guids(guid_values)
            if login_values:
                by_login = ad.users_by_logins(login_values)
        except Exception as exc:
            message = f"AD: {exc}"
            for record in records:
                if record.worker_key not in hints:
                    continue
                record.ad_status = "error"
                record.reconciliation_error = message
                self._recalculate_status(record)
            self.db.commit()
            return 0

        resolved = 0
        for record in records:
            hint = hints.get(record.worker_key)
            if hint is None:
                continue

            user = None
            guid = hint.get("guid", "")
            login = hint.get("login", "")
            if guid:
                user = by_guid.get(guid)
            if user is None and login:
                user = by_login.get(login)

            if user is None:
                # A hint exists but AD no longer has that identity.
                record.ad_status = "missing"
                self._recalculate_status(record)
                continue

            record.ad_status = (
                "enabled" if user.is_enabled else "disabled"
            )
            # The field is a candidate login. For an empty source value it is
            # safe to retain the actual shared AD login discovered by worker_key.
            if not record.login.strip():
                record.login = user.username
            self._recalculate_status(record)
            resolved += 1

        self.db.commit()
        return resolved

    def reconcile_all(self) -> dict:
        """Пересверить все текущие организации, затем применить общий AD."""
        sources = self._source_ids_with_records()
        details: list[dict] = []
        errors: list[dict] = []

        for source_id in sources:
            try:
                service = self._service_for(source_id)
                service.reconcile_current()
                shared_ad_resolved = self._resolve_shared_ad_for_source(
                    source_id
                )
                details.append(
                    {
                        "source_id": source_id,
                        "shared_ad_resolved": shared_ad_resolved,
                    }
                )
            except Exception as exc:
                self.db.rollback()
                errors.append(
                    {
                        "source_id": source_id,
                        "error": str(exc),
                    }
                )

        return {
            "summary": self.summary(),
            "sources": details,
            "errors": errors,
        }

    def summary(self, *, source_id: str = "") -> dict[str, int | str]:
        source_id = str(source_id or "").strip().lower()
        sources = self._source_ids_with_records()
        configured = self._configured_sources()

        if source_id:
            if source_id not in sources:
                return self._empty_summary(
                    source_id=source_id,
                    source_name=configured.get(source_id, source_id),
                )
            result = dict(self._service_for(source_id).summary())
            result["source_id"] = source_id
            result["source_name"] = configured.get(
                source_id,
                str(result.get("source_name") or source_id),
            )
            result["organizations"] = 1
            return result

        if not sources:
            return self._empty_summary(
                source_id="",
                source_name="Все организации",
            )

        totals = {key: 0 for key in SUMMARY_KEYS}
        for domain in sources:
            part = self._service_for(domain).summary()
            for key in SUMMARY_KEYS:
                totals[key] += int(part.get(key, 0) or 0)

        return {
            "source_id": "",
            "source_name": "Все организации",
            "organizations": len(sources),
            **totals,
        }

    @staticmethod
    def _empty_summary(
        *,
        source_id: str,
        source_name: str,
    ) -> dict[str, int | str]:
        return {
            "source_id": source_id,
            "source_name": source_name,
            "organizations": 0,
            **{key: 0 for key in SUMMARY_KEYS},
        }

    def list_rows(
        self,
        *,
        query: str = "",
        status: str = "all",
        source_id: str = "",
        limit: int = 1000,
    ) -> list[dict]:
        selected = str(source_id or "").strip().lower()
        sources = self._source_ids_with_records()
        if selected:
            sources = [selected] if selected in sources else []

        configured = self._configured_sources()
        rows: list[dict] = []
        per_source_limit = max(1, int(limit))

        active_records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.is_present.is_(True)
                )
            ).all()
        )
        record_by_id = {record.id: record for record in active_records}
        worker_keys = list(
            {
                record.worker_key
                for record in active_records
            }
        )
        manual_mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key.in_(worker_keys)
                )
            ).all()
        ) if worker_keys else []
        mapping_by_pair = {
            (
                mapping.worker_key,
                str(mapping.source_domain or "").strip().lower(),
            ): mapping
            for mapping in manual_mappings
        }

        for domain in sources:
            shared_hints = self._shared_ad_hints(domain, worker_keys)
            part = self._service_for(domain).list_rows(
                query=query,
                status=status,
                limit=per_source_limit,
            )
            for item in part:
                row = dict(item)
                row["source_id"] = domain
                row["source_name"] = configured.get(
                    domain,
                    str(row.get("source_name") or domain),
                )
                record = record_by_id.get(int(row.get("id") or 0))
                mapping = None
                if record is not None:
                    mapping = mapping_by_pair.get(
                        (record.worker_key, domain)
                    )

                if (
                    record is not None
                    and not str(row.get("login") or "").strip()
                ):
                    hint = shared_hints.get(record.worker_key) or {}
                    shared_login = str(hint.get("login") or "").strip()
                    if shared_login:
                        row["login"] = shared_login
                        row["login_from_other_source"] = True

                row["linked_email"] = ""
                row["mapping_action_label"] = "Сопоставить"
                if mapping is not None:
                    mapped_email = str(
                        mapping.source_email
                        or mapping.zimbra_email
                        or ""
                    ).strip().lower()
                    source_email = str(
                        row.get("email") or ""
                    ).strip().lower()
                    if mapped_email and mapped_email != source_email:
                        row["linked_email"] = mapped_email
                    row["mapping_action_label"] = "Изменить"

                effective_status = str(
                    row.get("reconciliation_status") or ""
                )
                if (
                    mapping is not None
                    or effective_status
                    in {"issue", "error", "not_checked"}
                ):
                    row["mapping_url"] = (
                        f"/employees/registry/{row['id']}/map"
                    )
                rows.append(row)

        rows.sort(
            key=lambda item: (
                str(item.get("fio") or "").casefold(),
                str(item.get("source_name") or "").casefold(),
                str(item.get("email") or "").casefold(),
            )
        )
        return rows[: max(1, int(limit))]
