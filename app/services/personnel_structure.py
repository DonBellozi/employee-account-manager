from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HRSourceRecord
from app.models_onec_sources import HREmploymentState


ACTIVE_EMPLOYMENT_STATUSES = {"active", "scheduled"}


def normalize_domain(value: str) -> str:
    return str(value or "").strip().lower().rstrip(".")


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def email_domain(value: str) -> str:
    email = normalize_email(value)
    if email.count("@") != 1:
        return ""
    local, domain = email.rsplit("@", 1)
    if not local or not domain:
        return ""
    return normalize_domain(domain)


@dataclass(frozen=True)
class OrganisationEmployment:
    worker_key: str
    source_id: str
    source_name: str
    status: str
    is_present: bool
    dismissal_date: date | None
    corporate_email: str = ""

    @property
    def domain(self) -> str:
        # В кадровом контуре source_id является доменом организации.
        return normalize_domain(self.source_id)

    @property
    def active(self) -> bool:
        return bool(
            self.is_present
            or self.status.strip().lower() in ACTIVE_EMPLOYMENT_STATUSES
        )


@dataclass(frozen=True)
class WorkerPersonnelState:
    worker_key: str
    fio: str
    employments: tuple[OrganisationEmployment, ...]

    @property
    def active_anywhere(self) -> bool:
        return any(item.active for item in self.employments)

    @property
    def final_dismissed(self) -> bool:
        return bool(self.employments) and not self.active_anywhere

    @property
    def final_dismissal_date(self) -> date | None:
        dates = [
            item.dismissal_date
            for item in self.employments
            if item.dismissal_date is not None
        ]
        return max(dates) if dates else None

    def employment_for_domain(self, domain: str) -> OrganisationEmployment | None:
        wanted = normalize_domain(domain)
        if not wanted:
            return None
        matches = [item for item in self.employments if item.domain == wanted]
        if not matches:
            return None
        # На практике worker_key + source_id уникальны. На случай старых дублей
        # действующая запись всегда имеет приоритет.
        matches.sort(key=lambda item: (not item.active, item.source_id))
        return matches[0]


@dataclass(frozen=True)
class EmailPersonnelState:
    email: str
    domain: str
    matched: bool
    worker_key: str = ""
    fio: str = ""
    active_in_domain: bool = False
    dismissal_date: date | None = None


class PersonnelStructureService:
    """Единое кадровое представление для всех интеграционных модулей.

    Сервис ничего не блокирует. Он только отвечает на кадровые вопросы:
    где человек работает, из какой организации уволен и принадлежит ли
    конкретный корпоративный e-mail действующему работнику этой организации.

    AD может использовать ``active_anywhere``. DSM и будущий 1С ДО используют
    состояние конкретного домена. Zimbra поверх этой структуры принимает
    собственные решения по физическому ящику, primary-адресам и alias.
    """

    def __init__(self, db: Session):
        self.db = db
        self._records: list[HRSourceRecord] | None = None
        self._states: list[HREmploymentState] | None = None

    def _load(self) -> None:
        if self._records is not None:
            return
        self._records = list(self.db.scalars(select(HRSourceRecord)).all())
        self._states = list(self.db.scalars(select(HREmploymentState)).all())

    def worker_state(self, worker_key: str) -> WorkerPersonnelState:
        self._load()
        assert self._records is not None
        assert self._states is not None

        key = str(worker_key or "").strip()
        if not key:
            return WorkerPersonnelState("", "", tuple())

        records = [row for row in self._records if str(row.worker_key or "").strip() == key]
        states = [row for row in self._states if str(row.worker_key or "").strip() == key]

        record_by_source = {
            normalize_domain(row.source_id): row
            for row in records
            if normalize_domain(row.source_id)
        }
        state_by_source = {
            normalize_domain(row.source_id): row
            for row in states
            if normalize_domain(row.source_id)
        }
        source_ids = sorted(set(record_by_source) | set(state_by_source))

        employments: list[OrganisationEmployment] = []
        for source_id in source_ids:
            record = record_by_source.get(source_id)
            state = state_by_source.get(source_id)
            status = str(getattr(state, "status", "") or "").strip().lower()
            is_present = bool(
                getattr(state, "is_present", False)
                if state is not None
                else getattr(record, "is_present", False)
            )
            # Старые БД могли иметь HRSourceRecord до появления employment-state.
            if not status:
                status = "active" if is_present else "dismissed"
            employments.append(
                OrganisationEmployment(
                    worker_key=key,
                    source_id=source_id,
                    source_name=str(
                        getattr(state, "source_name", "")
                        or getattr(record, "source_name", "")
                        or source_id
                    ),
                    status=status,
                    is_present=is_present,
                    dismissal_date=getattr(state, "dismissal_date", None),
                    corporate_email=normalize_email(
                        getattr(record, "corporate_email", "")
                    ),
                )
            )

        fio = ""
        for row in states + records:
            candidate = " ".join(str(getattr(row, "fio", "") or "").split())
            if candidate:
                fio = candidate
                break

        return WorkerPersonnelState(key, fio, tuple(employments))

    def email_state(self, email: str) -> EmailPersonnelState:
        """Проверить именно e-mail в его организации, без догадок по ФИО/login.

        Это принципиально для DSM: ``user@domain2.ru`` защищается только если
        такой адрес относится к действующему работнику domain2. Работа того же
        человека в domain1 не защищает эту локальную учетную запись.
        """
        self._load()
        assert self._records is not None

        target = normalize_email(email)
        domain = email_domain(target)
        if not target or not domain:
            return EmailPersonnelState(target, domain, False)

        matches = [
            row
            for row in self._records
            if normalize_email(row.corporate_email) == target
            and normalize_domain(row.source_id) == domain
        ]
        if not matches:
            return EmailPersonnelState(target, domain, False)

        worker_keys = {
            str(row.worker_key or "").strip()
            for row in matches
            if str(row.worker_key or "").strip()
        }
        if len(worker_keys) != 1:
            # Конфликт данных не должен давать ложное подтверждение активности.
            return EmailPersonnelState(target, domain, True)

        worker_key = next(iter(worker_keys))
        worker = self.worker_state(worker_key)
        employment = worker.employment_for_domain(domain)
        return EmailPersonnelState(
            email=target,
            domain=domain,
            matched=True,
            worker_key=worker_key,
            fio=worker.fio,
            active_in_domain=bool(employment and employment.active),
            dismissal_date=(employment.dismissal_date if employment else None),
        )

    def active_anywhere(self, worker_key: str) -> bool:
        return self.worker_state(worker_key).active_anywhere

    def active_in_domain(self, worker_key: str, domain: str) -> bool:
        employment = self.worker_state(worker_key).employment_for_domain(domain)
        return bool(employment and employment.active)
