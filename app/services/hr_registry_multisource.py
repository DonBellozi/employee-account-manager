from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import HRSourceRecord
from app.models_onec_sources import OneCAdditionalSource
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

        for domain in sources:
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
                rows.append(row)

        rows.sort(
            key=lambda item: (
                str(item.get("fio") or "").casefold(),
                str(item.get("source_name") or "").casefold(),
                str(item.get("email") or "").casefold(),
            )
        )
        return rows[: max(1, int(limit))]
