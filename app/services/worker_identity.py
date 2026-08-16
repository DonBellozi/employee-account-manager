from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailLoginMapping, HRSourceRecord
from app.services.personnel_structure import PersonnelStructureService, email_domain


"""Единое правило «чей это объект».

Модуль отвечает только за идентификацию человека. Кадровое состояние живет в
``PersonnelStructureService`` и не должно заново вычисляться каждой интеграцией.

Важно: если внешний объект уже содержит конкретный e-mail, отсутствие этого
адреса в кадровых данных не разрешает затем «спасти» его совпадением ФИО или
логина. Для DSM это означало бы, что ``user@domain2.ru`` продолжает работать
только потому, что тот же человек найден в другой организации. Поэтому
fallback на login/FIO выполняется только когда пригодного e-mail у объекта нет.
"""


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def normalize_login(value: str) -> str:
    return str(value or "").strip().casefold()


def normalize_fio(value: str) -> str:
    text = " ".join(str(value or "").split()).casefold()
    return text.replace("ё", "е")


def email_local_part(value: str) -> str:
    email = normalize_email(value)
    if email.count("@") != 1:
        return ""
    local, _domain = email.rsplit("@", 1)
    return local


@dataclass(frozen=True)
class IdentityMatch:
    worker_keys: frozenset[str] = field(default_factory=frozenset)
    method: str = ""
    active: bool = False
    dismissal_date: date | None = None

    @property
    def matched(self) -> bool:
        return bool(self.worker_keys)

    @property
    def ambiguous(self) -> bool:
        return len(self.worker_keys) > 1

    @property
    def worker_key(self) -> str:
        if len(self.worker_keys) != 1:
            return ""
        return next(iter(self.worker_keys))


class WorkerIdentityResolver:
    """Сопоставляет объект внешней системы с кадровым человеком.

    Адреса остаются наиболее надежным признаком. Если объект содержит хотя бы
    один непустой e-mail и ни один адрес не найден, resolver не переходит к
    логину/FIO. Если e-mail у объекта отсутствует, допускается поиск по логину,
    затем по ФИО – он полезен для интерфейсного сопоставления, но не меняет
    доменную политику DSM.
    """

    def __init__(self, db: Session):
        self.db = db
        self._by_email: dict[str, set[str]] | None = None
        self._by_login: dict[str, set[str]] | None = None
        self._by_fio: dict[str, set[str]] | None = None
        self._personnel = PersonnelStructureService(db)

    def _build(self) -> None:
        if self._by_email is not None:
            return

        by_email: dict[str, set[str]] = {}
        by_login: dict[str, set[str]] = {}
        by_fio: dict[str, set[str]] = {}

        def add(index: dict[str, set[str]], key: str, worker_key: str) -> None:
            if not key or not worker_key:
                return
            index.setdefault(key, set()).add(worker_key)

        records = list(self.db.scalars(select(HRSourceRecord)).all())
        for record in records:
            worker_key = str(record.worker_key or "").strip()
            if not worker_key:
                continue
            add(by_email, normalize_email(record.corporate_email), worker_key)
            add(by_email, normalize_email(record.personal_email), worker_key)
            add(by_login, normalize_login(record.login), worker_key)
            add(
                by_login,
                normalize_login(email_local_part(record.corporate_email)),
                worker_key,
            )
            add(by_fio, normalize_fio(record.fio), worker_key)

        mappings = list(self.db.scalars(select(EmailLoginMapping)).all())
        for mapping in mappings:
            worker_key = str(mapping.worker_key or "").strip()
            if not worker_key:
                continue
            add(by_email, normalize_email(mapping.source_email), worker_key)
            add(by_email, normalize_email(mapping.zimbra_email), worker_key)
            add(by_login, normalize_login(mapping.ad_login), worker_key)

        self._by_email = by_email
        self._by_login = by_login
        self._by_fio = by_fio

    def resolve(
        self,
        *,
        emails: object = (),
        logins: object = (),
        fio: str = "",
        include_email_local_parts: bool = True,
    ) -> IdentityMatch:
        self._build()
        assert self._by_email is not None
        assert self._by_login is not None
        assert self._by_fio is not None

        if isinstance(emails, str):
            emails = [emails]
        if isinstance(logins, str):
            logins = [logins]

        normalized_emails = [
            value for value in (normalize_email(item) for item in emails) if value
        ]

        # Конкретный адрес – сильный организационный признак. Если он есть, мы
        # не подменяем его совпавшим логином/FIO другой организации.
        if normalized_emails:
            keys: set[str] = set()
            for candidate in normalized_emails:
                keys.update(self._by_email.get(candidate, set()))
            if keys:
                return self._match_email(keys, normalized_emails)
            return IdentityMatch()

        normalized_logins = [
            value for value in (normalize_login(item) for item in logins) if value
        ]
        if include_email_local_parts:
            normalized_logins.extend(
                value
                for value in (
                    normalize_login(email_local_part(item))
                    for item in normalized_emails
                )
                if value
            )

        keys: set[str] = set()
        for candidate in normalized_logins:
            keys.update(self._by_login.get(candidate, set()))
        if keys:
            return self._match(keys, "login")

        fio_key = normalize_fio(fio)
        if fio_key:
            keys = set(self._by_fio.get(fio_key, set()))
            if keys:
                return self._match(keys, "fio")

        return IdentityMatch()


    def _match_email(self, keys: set[str], emails: list[str]) -> IdentityMatch:
        """Состояние e-mail оценивается в организации его домена.

        Один и тот же worker_key может оставаться ACTIVE в domain1 и быть
        DISMISSED в domain2. Это защищает общую AD-учетку, но не должно
        защищать DSM/Zimbra-адрес domain2.
        """
        active = False
        dates: list[date] = []
        for key in keys:
            worker = self._personnel.worker_state(key)
            for email in emails:
                domain = email_domain(email)
                if not domain:
                    continue
                employment = worker.employment_for_domain(domain)
                if employment is None:
                    continue
                if employment.active:
                    active = True
                if employment.dismissal_date is not None:
                    dates.append(employment.dismissal_date)
        return IdentityMatch(
            worker_keys=frozenset(keys),
            method="email_ambiguous" if len(keys) > 1 else "email",
            active=active,
            dismissal_date=max(dates) if dates else None,
        )

    def _match(self, keys: set[str], method: str) -> IdentityMatch:
        active = any(self._personnel.active_anywhere(key) for key in keys)
        dates = [
            state.final_dismissal_date
            for state in (self._personnel.worker_state(key) for key in keys)
            if state.final_dismissal_date is not None
        ]
        return IdentityMatch(
            worker_keys=frozenset(keys),
            method=f"{method}_ambiguous" if len(keys) > 1 else method,
            active=active,
            dismissal_date=max(dates) if dates else None,
        )

    def is_active_worker(self, **kwargs) -> bool:
        return self.resolve(**kwargs).active
