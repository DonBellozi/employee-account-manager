from __future__ import annotations

import json
import re
import shlex
import threading
import time
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DismissalSchedule, HRSourceRecord, OneCImportRun
from app.models_zimbra_observer import (
    ZimbraLifecycleState,
    ZimbraObservationEvent,
    ZimbraObservationRun,
    ZimbraObserverSettings,
)
from app.services.zimbra import ZimbraService


HR_SNAPSHOT_MAX_AGE_HOURS = 36
SUCCESSFUL_HR_STATUSES = {"success", "partial", "duplicate"}
DATE_RE = re.compile(r"(?<!\d)(\d{1,2}\.\d{1,2}\.\d{4})(?!\d)")
DISMISSAL_WORD_RE = re.compile(r"увол(?:ен|ьнение|ьняется|ить)", re.IGNORECASE)
NEVER_DISABLE_RE = re.compile(r"(?:^|[^a-z0-9_])never_disable(?:$|[^a-z0-9_])", re.IGNORECASE)
SCHEDULE_RE = re.compile(r"^(\d{2}):(\d{2})$")

RECOMMENDATION_LABELS = {
    "none": "Действий не требуется",
    "close": "Рекомендуется закрыть Zimbra",
    "archive_delete": "Рекомендуется архивировать и удалить",
    "protected_hr": "Действие подавлено данными 1С",
    "protected_note": "Защищена never_disable",
    "manual_review": "Требует проверки",
    "missing": "Учетная запись больше не найдена",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def subtract_months(value: datetime, months: int) -> datetime:
    months = max(0, int(months))
    total = value.year * 12 + (value.month - 1) - months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def parse_zimbra_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.match(r"^(\d{14})(?:\.\d+)?Z$", text)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def parse_note_date(value: str) -> date | None:
    match = DATE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_dismissal_note_date(value: str) -> date | None:
    text = str(value or "").strip()
    note_date = parse_note_date(text)
    if note_date is None:
        return None
    if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", text):
        return note_date
    if DISMISSAL_WORD_RE.search(text):
        return note_date
    return None


def date_to_utc(value: date, timezone_name: str) -> datetime:
    local = datetime.combine(value, dt_time.min, tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc)


def format_date(value: datetime | date | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    return value.strftime("%d.%m.%Y")


@dataclass(frozen=True)
class ObservedZimbraAccount:
    zimbra_id: str
    primary_email: str
    addresses: tuple[str, ...]
    account_status: str
    last_logon_at: datetime | None
    created_at: datetime | None
    note: str

    @property
    def account_key(self) -> str:
        return self.zimbra_id.strip() or self.primary_email.strip().lower()


@dataclass(frozen=True)
class HRProtectionSnapshot:
    emails: frozenset[str]
    snapshot_at: datetime | None
    age_minutes: int | None
    fresh: bool
    records_count: int


@dataclass(frozen=True)
class Evaluation:
    recommendation: str
    reason: str
    hr_active: bool = False
    matched_hr_email: str = ""
    first_observed_closed_at: datetime | None = None


class ZimbraObserverService:
    """Read-only анализ жизненного цикла учетных записей Zimbra.

    Этот сервис намеренно не содержит команд modifyAccount/deleteAccount,
    резервного копирования и Telegram. Он только читает Zimbra/1С и пишет
    выводы в локальный журнал наблюдения.
    """

    _run_lock = threading.Lock()

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def get_settings_record(self) -> ZimbraObserverSettings:
        record = self.db.get(ZimbraObserverSettings, 1)
        if record is None:
            record = ZimbraObserverSettings(
                id=1,
                enabled=True,
                inactive_months=6,
                retention_months=12,
                schedule_time="08:30",
                exclude_active_hr=True,
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
        return record

    def settings_view(self) -> dict[str, object]:
        record = self.get_settings_record()
        return {
            "enabled": bool(record.enabled),
            "inactive_months": int(record.inactive_months),
            "retention_months": int(record.retention_months),
            "schedule_time": record.schedule_time,
            "exclude_active_hr": bool(record.exclude_active_hr),
            "updated_by": record.updated_by,
            "updated_at": record.updated_at,
            "hr_freshness_hours": HR_SNAPSHOT_MAX_AGE_HOURS,
        }

    def save_settings(
        self,
        *,
        enabled: bool,
        inactive_months: int,
        retention_months: int,
        schedule_time: str,
        exclude_active_hr: bool,
        operator: str,
    ) -> ZimbraObserverSettings:
        inactive_months = int(inactive_months)
        retention_months = int(retention_months)
        if not 1 <= inactive_months <= 60:
            raise ValueError("Срок неактивности должен быть от 1 до 60 месяцев")
        if not 1 <= retention_months <= 120:
            raise ValueError("Срок хранения должен быть от 1 до 120 месяцев")

        match = SCHEDULE_RE.fullmatch(str(schedule_time or "").strip())
        if not match:
            raise ValueError("Время проверки должно быть в формате HH:MM")
        hour, minute = (int(part) for part in match.groups())
        if hour > 23 or minute > 59:
            raise ValueError("Указано недопустимое время проверки")
        normalized_time = f"{hour:02d}:{minute:02d}"

        record = self.get_settings_record()
        record.enabled = bool(enabled)
        record.inactive_months = inactive_months
        record.retention_months = retention_months
        record.schedule_time = normalized_time
        record.exclude_active_hr = bool(exclude_active_hr)
        record.updated_by = str(operator or "")[:256]
        record.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(record)
        return record

    def recent_runs(self, limit: int = 20) -> list[ZimbraObservationRun]:
        return list(
            self.db.scalars(
                select(ZimbraObservationRun)
                .order_by(desc(ZimbraObservationRun.started_at), desc(ZimbraObservationRun.id))
                .limit(max(1, min(int(limit), 100)))
            ).all()
        )

    def latest_run(self) -> ZimbraObservationRun | None:
        return self.db.scalars(
            select(ZimbraObservationRun)
            .order_by(desc(ZimbraObservationRun.started_at), desc(ZimbraObservationRun.id))
            .limit(1)
        ).first()

    def recent_events(self, limit: int = 50) -> list[ZimbraObservationEvent]:
        return list(
            self.db.scalars(
                select(ZimbraObservationEvent)
                .order_by(desc(ZimbraObservationEvent.created_at), desc(ZimbraObservationEvent.id))
                .limit(max(1, min(int(limit), 200)))
            ).all()
        )

    def current_states(self, limit: int = 200) -> list[ZimbraLifecycleState]:
        ordering = (
            case(
                (ZimbraLifecycleState.recommendation == "close", 1),
                (ZimbraLifecycleState.recommendation == "archive_delete", 2),
                (ZimbraLifecycleState.recommendation == "manual_review", 3),
                (ZimbraLifecycleState.recommendation == "protected_note", 4),
                (ZimbraLifecycleState.recommendation == "protected_hr", 5),
                (ZimbraLifecycleState.recommendation == "missing", 6),
                else_=9,
            ),
            ZimbraLifecycleState.primary_email,
        )
        return list(
            self.db.scalars(
                select(ZimbraLifecycleState)
                .where(ZimbraLifecycleState.recommendation != "none")
                .order_by(*ordering)
                .limit(max(1, min(int(limit), 1000)))
            ).all()
        )

    def run(self, trigger: str = "manual") -> ZimbraObservationRun:
        trigger = str(trigger or "manual").strip().lower()
        if trigger not in {"manual", "scheduled"}:
            raise ValueError("Неизвестный тип запуска наблюдения Zimbra")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Проверка Zimbra уже выполняется")

        run = ZimbraObservationRun(
            trigger=trigger,
            status="running",
            started_at=utcnow(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        try:
            config = self.get_settings_record()
            accounts = self._fetch_accounts()
            if not accounts:
                raise RuntimeError(
                    "Zimbra не вернула ни одной учетной записи в настроенных доменах"
                )

            hr = self._hr_snapshot()
            now = utcnow()
            local_today = now.astimezone(ZoneInfo(self.settings.app_timezone)).date()
            dismissal_map = self._dismissal_schedule_map()

            existing_states = {
                row.account_key: row
                for row in self.db.scalars(select(ZimbraLifecycleState)).all()
            }
            seen_keys: set[str] = set()
            counters = {
                "close": 0,
                "archive_delete": 0,
                "protected_hr": 0,
                "manual_review": 0,
                "events": 0,
            }

            for account in accounts:
                key = account.account_key
                if not key:
                    continue
                seen_keys.add(key)
                state = existing_states.get(key)
                evaluation = self._evaluate(
                    account,
                    previous_state=state,
                    config=config,
                    hr=hr,
                    dismissal_map=dismissal_map,
                    now=now,
                    local_today=local_today,
                )

                if evaluation.recommendation in counters:
                    counters[evaluation.recommendation] += 1

                if state is None:
                    state = ZimbraLifecycleState(
                        account_key=key,
                        first_observed_at=now,
                        last_changed_at=now,
                    )
                    self.db.add(state)
                    previous_recommendation = ""
                    previous_reason = ""
                    previous_status = ""
                else:
                    previous_recommendation = state.recommendation
                    previous_reason = state.reason
                    previous_status = state.account_status

                if previous_status == "closed" and account.account_status != "closed":
                    first_closed = None
                elif account.account_status == "closed":
                    first_closed = (
                        evaluation.first_observed_closed_at
                        or as_utc(state.first_observed_closed_at)
                        or now
                    )
                else:
                    first_closed = None

                changed = (
                    evaluation.recommendation != previous_recommendation
                    or (
                        evaluation.recommendation != "none"
                        and evaluation.reason != previous_reason
                    )
                )

                state.zimbra_id = account.zimbra_id
                state.primary_email = account.primary_email
                state.addresses_json = json.dumps(
                    list(account.addresses), ensure_ascii=False
                )
                state.account_status = account.account_status
                state.last_logon_at = account.last_logon_at
                state.created_in_zimbra_at = account.created_at
                state.zimbra_note = account.note
                state.hr_active = evaluation.hr_active
                state.matched_hr_email = evaluation.matched_hr_email
                state.recommendation = evaluation.recommendation
                state.reason = evaluation.reason
                state.first_observed_closed_at = first_closed
                state.last_observed_at = now
                if changed:
                    state.last_changed_at = now

                global_hr_stale_review = (
                    evaluation.recommendation == "manual_review"
                    and config.exclude_active_hr
                    and not hr.fresh
                    and "актуальность списка" in evaluation.reason
                )
                if changed and not global_hr_stale_review and (
                    previous_recommendation
                    or evaluation.recommendation != "none"
                ):
                    self._add_event(
                        run_id=run.id,
                        account=account,
                        previous_recommendation=previous_recommendation,
                        recommendation=evaluation.recommendation,
                        reason=evaluation.reason,
                        hr_active=evaluation.hr_active,
                        created_at=now,
                    )
                    counters["events"] += 1

            # Учетная запись исчезла из полного успешного чтения Zimbra.
            # Отмечаем это один раз, но ничего не удаляем и не считаем увольнением.
            for key, state in existing_states.items():
                if key in seen_keys:
                    continue
                if state.recommendation == "missing":
                    continue
                previous = state.recommendation
                reason = (
                    "Учетная запись больше не найдена при полном чтении настроенных "
                    "доменов Zimbra. Это наблюдение не считается увольнением и не "
                    "запускает никаких действий."
                )
                state.recommendation = "missing"
                state.reason = reason
                state.last_observed_at = now
                state.last_changed_at = now
                self._add_event(
                    run_id=run.id,
                    account=ObservedZimbraAccount(
                        zimbra_id=state.zimbra_id,
                        primary_email=state.primary_email,
                        addresses=tuple(),
                        account_status=state.account_status,
                        last_logon_at=as_utc(state.last_logon_at),
                        created_at=as_utc(state.created_in_zimbra_at),
                        note=state.zimbra_note,
                    ),
                    previous_recommendation=previous,
                    recommendation="missing",
                    reason=reason,
                    hr_active=state.hr_active,
                    created_at=now,
                )
                counters["events"] += 1

            run.status = "success" if hr.fresh or not config.exclude_active_hr else "warning"
            run.total_accounts = len(accounts)
            run.relevant_accounts = len(seen_keys)
            run.close_candidates = counters["close"]
            run.archive_candidates = counters["archive_delete"]
            run.protected_by_hr = counters["protected_hr"]
            run.manual_review = counters["manual_review"]
            run.event_count = counters["events"]
            run.hr_snapshot_at = hr.snapshot_at
            run.hr_snapshot_age_minutes = hr.age_minutes
            if config.exclude_active_hr and not hr.fresh:
                run.error_message = (
                    "Актуальность кадровой выгрузки 1С не подтверждена. "
                    "Рискованные рекомендации переведены в «Требует проверки»."
                )
            run.completed_at = utcnow()
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            failed_run = self.db.get(ZimbraObservationRun, run.id)
            if failed_run is not None:
                failed_run.status = "failed"
                failed_run.error_message = str(exc)[:4000]
                failed_run.completed_at = utcnow()
                self.db.commit()
                self.db.refresh(failed_run)
                return failed_run
            raise
        finally:
            self._run_lock.release()

    def _fetch_accounts(self) -> list[ObservedZimbraAccount]:
        domains = [
            item.strip().lower()
            for item in self.settings.zimbra_domains
            if item.strip()
        ]
        if not domains and self.settings.zimbra_primary_domain.strip():
            domains = [self.settings.zimbra_primary_domain.strip().lower()]
        if not domains:
            raise RuntimeError("Не настроены домены Zimbra")
        if self.settings.zimbra_backend == "disabled":
            raise RuntimeError("Zimbra backend отключен")

        service = ZimbraService(self.settings)
        # Общий lock ZimbraService не дает ручной сверке/созданию одновременно
        # запускать несколько тяжелых JVM-процессов zmprov на сервере Zimbra.
        with service._query_lock:
            client = service._client()  # read-only использование общей SSH-конфигурации
            try:
                combined_accounts: dict[str, ObservedZimbraAccount] = {}
                for domain in domains:
                    command = f"{service._zmprov_command()} -l gaa -v {shlex.quote(domain)}"
                    stdin, stdout, stderr = client.exec_command(command, timeout=120)
                    stdin.channel.shutdown_write()
                    channel = stdout.channel
                    deadline = time.monotonic() + 120.0
                    out_chunks: list[bytes] = []
                    err_chunks: list[bytes] = []

                    # gaa -v может вернуть много данных. Вычитываем канал в процессе
                    # выполнения, чтобы удаленный процесс не остановился на полном
                    # SSH-буфере до того, как успеет завершиться.
                    while True:
                        made_progress = False
                        while channel.recv_ready():
                            out_chunks.append(channel.recv(65536))
                            made_progress = True
                        while channel.recv_stderr_ready():
                            err_chunks.append(channel.recv_stderr(65536))
                            made_progress = True

                        if (
                            channel.exit_status_ready()
                            and not channel.recv_ready()
                            and not channel.recv_stderr_ready()
                        ):
                            break
                        if time.monotonic() >= deadline:
                            channel.close()
                            raise RuntimeError(
                                f"Превышено время чтения учетных записей Zimbra ({domain})"
                            )
                        if not made_progress:
                            time.sleep(0.05)

                    code = channel.recv_exit_status()
                    output = b"".join(out_chunks).decode("utf-8", errors="replace")
                    error = b"".join(err_chunks).decode(
                        "utf-8", errors="replace"
                    ).strip()
                    if code != 0:
                        raise RuntimeError(
                            f"Не удалось прочитать учетные записи Zimbra ({domain}): "
                            f"{error or f'код {code}'}"
                        )
                    for account in self._parse_gaa_verbose(output):
                        key = account.zimbra_id or account.primary_email
                        if key:
                            combined_accounts[key.lower()] = account
                return list(combined_accounts.values())
            finally:
                client.close()

    @classmethod
    def _parse_gaa_verbose(cls, output: str) -> list[ObservedZimbraAccount]:
        accounts: list[ObservedZimbraAccount] = []
        current_name = ""
        attrs: dict[str, list[str]] = {}

        def flush() -> None:
            nonlocal current_name, attrs
            if not current_name and not attrs:
                return
            primary = cls._first_attr(
                attrs,
                "mail",
                "zimbramaildeliveryaddress",
            ) or current_name
            primary = primary.strip().lower()
            if not primary or "@" not in primary:
                current_name = ""
                attrs = {}
                return

            addresses: list[str] = []
            for attr_name in (
                "mail",
                "zimbramaildeliveryaddress",
                "zimbramailalias",
            ):
                for value in attrs.get(attr_name, []):
                    normalized = value.strip().lower()
                    if normalized and "@" in normalized and normalized not in addresses:
                        addresses.append(normalized)
            if primary not in addresses:
                addresses.insert(0, primary)

            accounts.append(
                ObservedZimbraAccount(
                    zimbra_id=cls._first_attr(attrs, "zimbraid"),
                    primary_email=primary,
                    addresses=tuple(addresses),
                    account_status=(
                        cls._first_attr(attrs, "zimbraaccountstatus").lower()
                        or "unknown"
                    ),
                    last_logon_at=parse_zimbra_timestamp(
                        cls._first_attr(attrs, "zimbralastlogontimestamp")
                    ),
                    created_at=parse_zimbra_timestamp(
                        cls._first_attr(attrs, "zimbracreatetimestamp")
                    ),
                    note=cls._first_attr(attrs, "zimbranotes"),
                )
            )
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
                continue
            # Защита на случай, если конкретная версия Zimbra вернет простой
            # список адресов даже с -v. Такие записи попадут в manual_review,
            # а не в автоматическую рекомендацию.
            stripped = line.strip().lower()
            if "@" in stripped and " " not in stripped:
                flush()
                current_name = stripped
                flush()
        flush()
        return accounts

    @staticmethod
    def _first_attr(attrs: dict[str, list[str]], *names: str) -> str:
        for name in names:
            for value in attrs.get(name, []):
                if value.strip():
                    return value.strip()
        return ""

    def _hr_snapshot(self) -> HRProtectionSnapshot:
        rows = self.db.scalars(
            select(HRSourceRecord).where(HRSourceRecord.is_present.is_(True))
        ).all()
        emails = frozenset(
            row.corporate_email.strip().lower()
            for row in rows
            if row.corporate_email and "@" in row.corporate_email
        )

        latest = self.db.scalars(
            select(OneCImportRun)
            .where(OneCImportRun.status.in_(SUCCESSFUL_HR_STATUSES))
            .order_by(desc(OneCImportRun.completed_at), desc(OneCImportRun.id))
            .limit(1)
        ).first()
        snapshot_at = as_utc(
            (latest.completed_at or latest.started_at) if latest is not None else None
        )
        if snapshot_at is None:
            age_minutes = None
        else:
            age_minutes = max(
                0,
                int((utcnow() - snapshot_at).total_seconds() // 60),
            )
        fresh = bool(
            snapshot_at is not None
            and age_minutes is not None
            and age_minutes <= HR_SNAPSHOT_MAX_AGE_HOURS * 60
            and rows
        )
        return HRProtectionSnapshot(
            emails=emails,
            snapshot_at=snapshot_at,
            age_minutes=age_minutes,
            fresh=fresh,
            records_count=len(rows),
        )

    def _dismissal_schedule_map(self) -> dict[str, date]:
        result: dict[str, date] = {}
        rows = self.db.scalars(
            select(DismissalSchedule).order_by(desc(DismissalSchedule.id))
        ).all()
        for row in rows:
            email = str(row.corporate_email or "").strip().lower()
            if not email or email in result:
                continue
            result[email] = row.dismissal_date
        return result

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
        addresses = tuple(address.strip().lower() for address in account.addresses)
        matched_hr = next((address for address in addresses if address in hr.emails), "")
        hr_active = bool(matched_hr)

        scheduled_dismissal = next(
            (
                dismissal_map[address]
                for address in addresses
                if address in dismissal_map
            ),
            None,
        )
        note_dismissal = parse_dismissal_note_date(account.note)
        dismissal_date = scheduled_dismissal or note_dismissal

        status = (account.account_status or "unknown").strip().lower()

        # never_disable – безусловный административный запрет на жизненный цикл.
        # Он имеет приоритет над увольнением, неактивностью, статусом closed и
        # сроком хранения: наблюдатель не должен даже рекомендовать закрытие
        # или удаление такой учетной записи. Сравнение намеренно
        # регистронезависимое, но ищет отдельный маркер, а не часть другого слова.
        if NEVER_DISABLE_RE.search(str(account.note or "")):
            return Evaluation(
                recommendation="protected_note",
                reason=(
                    "В zimbraNotes установлен never_disable. Учетная запись "
                    "исключена из закрытия, архивации и удаления независимо от "
                    "неактивности, даты увольнения и срока хранения."
                ),
                hr_active=hr_active,
                matched_hr_email=matched_hr,
                first_observed_closed_at=(
                    as_utc(previous_state.first_observed_closed_at)
                    if previous_state is not None
                    else None
                ),
            )

        activity = account.last_logon_at or account.created_at
        inactivity_cutoff = subtract_months(now, config.inactive_months)
        inactive = bool(activity is not None and activity <= inactivity_cutoff)

        if status == "active":
            if dismissal_date is not None and dismissal_date <= local_today:
                return Evaluation(
                    recommendation="close",
                    reason=f"Увольнение {dismissal_date.strftime('%d.%m.%Y')}. Учетная запись Zimbra остается активной.",
                    hr_active=hr_active,
                    matched_hr_email=matched_hr,
                )

            if activity is None:
                return Evaluation(
                    recommendation="manual_review",
                    reason=(
                        "Zimbra не вернула ни zimbraLastLogonTimestamp, ни дату создания. "
                        "Срок неактивности определить нельзя."
                    ),
                    hr_active=hr_active,
                    matched_hr_email=matched_hr,
                )

            if not inactive:
                return Evaluation(
                    recommendation="none",
                    reason=(
                        f"Последняя активность {format_date(activity)} – срок "
                        f"{config.inactive_months} мес. не превышен."
                    ),
                    hr_active=hr_active,
                    matched_hr_email=matched_hr,
                )

            if config.exclude_active_hr and not hr.fresh:
                return Evaluation(
                    recommendation="manual_review",
                    reason=(
                        f"Неактивна с {format_date(activity)}, но актуальность списка "
                        f"действующих работников 1С не подтверждена (допуск {HR_SNAPSHOT_MAX_AGE_HOURS} ч.). "
                        "Рекомендация закрытия намеренно не формируется."
                    ),
                    hr_active=hr_active,
                    matched_hr_email=matched_hr,
                )

            if config.exclude_active_hr and hr_active:
                return Evaluation(
                    recommendation="protected_hr",
                    reason=(
                        f"Неактивна с {format_date(activity)}, но адрес {matched_hr} "
                        "найден у действующего работника в актуальном реестре 1С. Закрытие подавлено."
                    ),
                    hr_active=True,
                    matched_hr_email=matched_hr,
                )

            activity_kind = (
                "Последняя активность"
                if account.last_logon_at is not None
                else "Входов не было, учетная запись создана"
            )
            return Evaluation(
                recommendation="close",
                reason=(
                    f"{activity_kind} {format_date(activity)}. Превышен срок "
                    f"неактивности {config.inactive_months} мес."
                ),
                hr_active=hr_active,
                matched_hr_email=matched_hr,
            )

        if status == "closed":
            if config.exclude_active_hr and hr_active:
                return Evaluation(
                    recommendation="manual_review",
                    reason=(
                        f"Учетная запись уже закрыта, но адрес {matched_hr} найден "
                        "у действующего работника в последнем реестре 1С."
                    ),
                    hr_active=True,
                    matched_hr_email=matched_hr,
                    first_observed_closed_at=(
                        as_utc(previous_state.first_observed_closed_at)
                        if previous_state is not None
                        else now
                    ),
                )

            note_date = parse_note_date(account.note)
            first_closed = (
                as_utc(previous_state.first_observed_closed_at)
                if previous_state is not None
                else None
            ) or now
            if note_date is not None:
                retention_basis = date_to_utc(note_date, self.settings.app_timezone)
                basis_text = (
                    f"дата {note_date.strftime('%d.%m.%Y')} из zimbraNotes"
                )
            else:
                retention_basis = first_closed
                basis_text = (
                    f"первое наблюдение статуса closed {format_date(first_closed)}"
                )

            retention_cutoff = subtract_months(now, config.retention_months)
            retention_due = retention_basis <= retention_cutoff

            if not retention_due:
                return Evaluation(
                    recommendation="none",
                    reason=(
                        f"Учетная запись закрыта. Срок хранения {config.retention_months} мес. "
                        f"еще не истек; основание – {basis_text}."
                    ),
                    hr_active=hr_active,
                    matched_hr_email=matched_hr,
                    first_observed_closed_at=first_closed,
                )

            if config.exclude_active_hr and not hr.fresh:
                return Evaluation(
                    recommendation="manual_review",
                    reason=(
                        f"Срок хранения {config.retention_months} мес. истек ({basis_text}), "
                        "но актуальность списка действующих работников 1С не подтверждена. "
                        "Архивация/удаление намеренно не рекомендуются."
                    ),
                    hr_active=hr_active,
                    matched_hr_email=matched_hr,
                    first_observed_closed_at=first_closed,
                )

            return Evaluation(
                recommendation="archive_delete",
                reason=(
                    f"Учетная запись закрыта, срок хранения {config.retention_months} мес. "
                    f"истек; основание – {basis_text}. Перед удалением требуется успешная "
                    "резервная копия. Сейчас никаких действий не выполняется."
                ),
                hr_active=hr_active,
                matched_hr_email=matched_hr,
                first_observed_closed_at=first_closed,
            )

        return Evaluation(
            recommendation="manual_review",
            reason=(
                f"Нештатный статус Zimbra «{status or 'не указан'}». Наблюдатель "
                "не формирует автоматических рекомендаций для этого состояния."
            ),
            hr_active=hr_active,
            matched_hr_email=matched_hr,
        )

    def _add_event(
        self,
        *,
        run_id: int,
        account: ObservedZimbraAccount,
        previous_recommendation: str,
        recommendation: str,
        reason: str,
        hr_active: bool,
        created_at: datetime,
    ) -> None:
        self.db.add(
            ZimbraObservationEvent(
                run_id=run_id,
                account_key=account.account_key,
                zimbra_id=account.zimbra_id,
                primary_email=account.primary_email,
                previous_recommendation=previous_recommendation,
                recommendation=recommendation,
                reason=reason,
                account_status=account.account_status,
                last_logon_at=account.last_logon_at,
                hr_active=hr_active,
                created_at=created_at,
            )
        )


def recommendation_label(value: str) -> str:
    return RECOMMENDATION_LABELS.get(value, value or "–")
