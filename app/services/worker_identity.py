from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import HREmploymentState


"""Единое правило «чей это объект».

Каждая интеграция наблюдает свой мир: Zimbra видит адреса, Synology — логины
и описания, AD — учетные записи. Кадровая выгрузка при этом остается
единственным источником правды о людях. Связать одно с другим приходится
всем, и раньше каждый контур делал это по-своему — по одному лишь совпадению
корпоративной почты.

Это дважды привело к отключению действующих работников: у одних e-mail в
системе не был заполнен, у других отличался от записанного в 1С. Поэтому
правило вынесено сюда и должно использоваться всеми контурами без исключения.

Главный принцип: отсутствие совпадения — это нехватка данных, а не
доказательство того, что человека нет. Трактуется в пользу работника.
"""


ACTIVE_EMPLOYMENT_STATUSES = {"active", "scheduled"}


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
    """Результат сопоставления объекта с человеком из кадровой выгрузки."""

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
        """Единственный ключ; при неоднозначности пусто."""
        if len(self.worker_keys) != 1:
            return ""
        return next(iter(self.worker_keys))


class WorkerIdentityResolver:
    """Сопоставляет объект внешней системы с работником по всем признакам.

    Индексы строятся один раз на экземпляр: резолвер рассчитан на прогон по
    сотням учетных записей подряд, и запрос на каждый признак каждой записи
    был бы неоправданно дорогим.
    """

    def __init__(self, db: Session):
        self.db = db
        self._by_email: dict[str, set[str]] | None = None
        self._by_login: dict[str, set[str]] | None = None
        self._by_fio: dict[str, set[str]] | None = None
        self._active: set[str] | None = None
        self._dismissals: dict[str, date] | None = None

    # --- построение индексов -------------------------------------------

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
            # Локальная часть корпоративного адреса — практически всегда и есть
            # логин человека, поэтому она тоже участвует в поиске.
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

        active: set[str] = {
            str(record.worker_key or "").strip()
            for record in records
            if record.is_present and str(record.worker_key or "").strip()
        }
        dismissals: dict[str, date] = {}
        states = list(self.db.scalars(select(HREmploymentState)).all())
        for state in states:
            worker_key = str(state.worker_key or "").strip()
            if not worker_key:
                continue
            # Продолжающаяся занятость хотя бы в одной организации защищает
            # общие учетные записи человека — то же правило, что в контуре
            # окончательного увольнения.
            if state.status in ACTIVE_EMPLOYMENT_STATUSES:
                active.add(worker_key)
            if state.dismissal_date is not None:
                current = dismissals.get(worker_key)
                if current is None or state.dismissal_date > current:
                    dismissals[worker_key] = state.dismissal_date

        self._by_email = by_email
        self._by_login = by_login
        self._by_fio = by_fio
        self._active = active
        self._dismissals = dismissals

    # --- сопоставление ---------------------------------------------------

    def resolve(
        self,
        *,
        emails: object = (),
        logins: object = (),
        fio: str = "",
        include_email_local_parts: bool = True,
    ) -> IdentityMatch:
        """Найти работника по любому из переданных признаков.

        Признаки проверяются по убыванию надежности: адреса, затем логины,
        затем ФИО. Первое непустое совпадение выигрывает.
        """
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

        for candidates, index, method in (
            (normalized_emails, self._by_email, "email"),
            (normalized_logins, self._by_login, "login"),
            ([normalize_fio(fio)], self._by_fio, "fio"),
        ):
            keys: set[str] = set()
            for candidate in candidates:
                if not candidate:
                    continue
                keys.update(index.get(candidate, set()))
            if keys:
                return self._match(keys, method)

        return IdentityMatch()

    def _match(self, keys: set[str], method: str) -> IdentityMatch:
        assert self._active is not None
        assert self._dismissals is not None
        active = any(key in self._active for key in keys)
        dates = [
            self._dismissals[key] for key in keys if key in self._dismissals
        ]
        return IdentityMatch(
            worker_keys=frozenset(keys),
            method=f"{method}_ambiguous" if len(keys) > 1 else method,
            active=active,
            dismissal_date=max(dates) if dates else None,
        )

    def is_active_worker(self, **kwargs) -> bool:
        """Короткая форма для контуров, которым нужен только факт занятости."""
        return self.resolve(**kwargs).active
