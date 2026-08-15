from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import paramiko

from app.config import Settings


@dataclass(frozen=True)
class SynologyLocalUser:
    login: str
    stable_id: str
    uid: str = ""
    email: str = ""
    description: str = ""
    status: str = "unknown"
    is_active: bool = True
    expires_at: date | None = None
    protected: bool = False
    detail_error: str = ""


@dataclass(frozen=True)
class SynologyDiagnostic:
    enum_output: str
    help_output: str
    sample_login: str = ""
    sample_detail: str = ""


class SynologyService:
    """SSH-клиент локальных пользователей Synology DSM.

    Чтение выполняется через штатный ``synouser``. На текущем write-этапе
    разрешено только безопасное отключение локальной учетки установкой
    ``Expired=1``. Включение, удаление и изменение других атрибутов здесь
    намеренно не реализованы.
    """

    SYSTEM_LOGINS = {
        "admin",
        "administrator",
        "guest",
        "root",
    }

    # DSM в конце `synouser --enum local` печатает, например,
    # `294 User Listed:`. Это счетчик, а не логин.
    ENUM_SUMMARY_RE = re.compile(r"^\s*\d+\s+users?\s+listed\s*:?\s*$", re.IGNORECASE)

    def __init__(self, settings: Settings):
        self.settings = settings

    def _read_ssh_password(self) -> str:
        if self.settings.synology_ssh_password_file:
            password_file = Path(self.settings.synology_ssh_password_file)
            if not password_file.is_file():
                raise RuntimeError("Не найден файл с SSH-паролем Synology")
            value = password_file.read_text(encoding="utf-8").rstrip("\r\n")
        else:
            value = self.settings.synology_ssh_password
        if not value:
            raise RuntimeError(
                "Не задан SSH-пароль Synology: укажите SYNOLOGY_SSH_PASSWORD "
                "или SYNOLOGY_SSH_PASSWORD_FILE"
            )
        return value

    def _resolve_auth(self) -> str:
        auth = self.settings.synology_ssh_auth
        if auth != "auto":
            return auth
        private_key = Path(self.settings.synology_ssh_private_key)
        if self.settings.synology_ssh_private_key and private_key.is_file():
            return "key"
        return "password"

    def _client(self) -> paramiko.SSHClient:
        if not self.settings.synology_ssh_host.strip():
            raise RuntimeError("Не задан SYNOLOGY_SSH_HOST")
        if not self.settings.synology_ssh_user.strip():
            raise RuntimeError("Не задан SYNOLOGY_SSH_USER")

        known_hosts = Path(self.settings.synology_ssh_known_hosts)
        if not known_hosts.is_file():
            raise RuntimeError("Не найден файл known_hosts для Synology")

        client = paramiko.SSHClient()
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        kwargs: dict[str, object] = {
            "hostname": self.settings.synology_ssh_host,
            "port": self.settings.synology_ssh_port,
            "username": self.settings.synology_ssh_user,
            "look_for_keys": False,
            "allow_agent": False,
            "timeout": self.settings.synology_connect_timeout_seconds,
            "banner_timeout": self.settings.synology_connect_timeout_seconds,
            "auth_timeout": self.settings.synology_connect_timeout_seconds,
        }

        auth = self._resolve_auth()
        if auth == "key":
            private_key = Path(self.settings.synology_ssh_private_key)
            if not private_key.is_file():
                raise RuntimeError("Не найден закрытый SSH-ключ Synology")
            kwargs["key_filename"] = str(private_key)
        elif auth == "password":
            kwargs["password"] = self._read_ssh_password()
        else:
            raise RuntimeError(
                f"Неизвестный режим SSH-аутентификации Synology: {auth}"
            )

        try:
            client.connect(**kwargs)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(15)
            return client
        except Exception:
            client.close()
            raise

    def _synouser_base(self) -> str:
        command = self.settings.synology_synouser_command.strip() or "synouser"
        quoted = shlex.quote(command)
        if self.settings.synology_ssh_use_sudo:
            return f"sudo -n {quoted}"
        return quoted

    # Верхняя граница вывода одной команды. Защищает фоновый поток от
    # неограниченного чтения, если DSM начнет печатать бесконечный поток.
    MAX_OUTPUT_BYTES = 4 * 1024 * 1024
    _POLL_INTERVAL_SECONDS = 0.02

    def _execute(
        self,
        client: paramiko.SSHClient,
        args: list[str],
        *,
        allow_nonzero: bool = False,
    ) -> str:
        """Выполнить synouser с жестким верхним пределом по времени.

        ``recv_exit_status()`` блокируется без таймаута: если DSM принял
        соединение, но не закрывает канал, фоновый поток сверки повисает
        навсегда и вместе с ним удерживается глобальный лок синхронизации.
        Поэтому ожидание завершения и чтение вывода выполняются циклом с
        собственным дедлайном.
        """
        timeout = max(5, self.settings.synology_command_timeout_seconds)
        command = f"{self._synouser_base()} {shlex.join(args)}"
        display = f"{self.settings.synology_synouser_command} {' '.join(args)}".strip()

        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        channel = stdout.channel
        try:
            channel.settimeout(timeout)
            stdin.channel.shutdown_write()

            deadline = time.monotonic() + timeout
            out_chunks: list[bytes] = []
            err_chunks: list[bytes] = []
            received = 0
            truncated = False

            while True:
                progressed = False
                if channel.recv_ready():
                    chunk = channel.recv(65536)
                    received += len(chunk)
                    if not truncated:
                        out_chunks.append(chunk)
                    progressed = True
                if channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(65536)
                    received += len(chunk)
                    if not truncated:
                        err_chunks.append(chunk)
                    progressed = True

                if received > self.MAX_OUTPUT_BYTES:
                    truncated = True

                # Команда завершена и буферы вычитаны.
                if not progressed and channel.exit_status_ready():
                    break

                # Дедлайн проверяется и при активном потоке вывода: иначе
                # бесконечно печатающая команда обошла бы таймаут.
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"synouser не ответил за {timeout} с: {display}"
                    )

                if not progressed:
                    time.sleep(self._POLL_INTERVAL_SECONDS)

            code = channel.recv_exit_status()
        finally:
            channel.close()

        out = b"".join(out_chunks).decode("utf-8", errors="replace").strip()
        err = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
        combined = "\n".join(part for part in (out, err) if part).strip()
        if truncated:
            combined = f"{combined}\n[вывод обрезан по лимиту приложения]".strip()
        if code != 0 and not allow_nonzero:
            raise RuntimeError(
                f"synouser завершился с кодом {code}: {combined or 'нет вывода'}"
            )
        return combined

    @staticmethod
    def _strip_wrapping(value: str) -> str:
        text = str(value or "").strip()
        if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
            text = text[1:-1].strip()
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            text = text[1:-1].strip()
        return text

    @classmethod
    def _parse_enum_output(cls, output: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw_line in str(output or "").splitlines():
            line = cls._strip_wrapping(raw_line)
            if not line:
                continue
            folded = line.casefold()
            if (
                "local user" in folded
                or "domain user" in folded
                or folded.startswith("total")
                or cls.ENUM_SUMMARY_RE.fullmatch(line) is not None
                or set(line) <= {"-", "=", " "}
            ):
                continue
            # Некоторые версии печатают "user: login".
            if ":" in line:
                left, right = line.split(":", 1)
                if left.strip().casefold() in {"user", "username", "name"}:
                    line = cls._strip_wrapping(right)
            login = line.strip()
            if not login or any(ch in login for ch in "\r\n\x00"):
                continue
            key = login.casefold()
            if key not in seen:
                seen.add(key)
                result.append(login)
        return result

    @staticmethod
    def _normalize_key(value: str) -> str:
        text = str(value or "").casefold().replace("_", " ").replace("-", " ")
        return " ".join(text.split())

    @classmethod
    def _detail_fields(cls, output: str) -> dict[str, list[str]]:
        fields: dict[str, list[str]] = {}
        for raw_line in str(output or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue

            match = re.match(r"^([^:=]+?)\s*[:=]\s*(.*)$", line)
            if match is None:
                continue
            key = cls._normalize_key(match.group(1))
            value = cls._strip_wrapping(match.group(2))
            fields.setdefault(key, []).append(value)
        return fields

    @staticmethod
    def _first(fields: dict[str, list[str]], *names: str) -> str:
        for name in names:
            values = fields.get(name)
            if values:
                for value in values:
                    if str(value or "").strip():
                        return str(value).strip()
        return ""

    @staticmethod
    def _as_bool(value: str) -> bool | None:
        text = str(value or "").strip().casefold()
        if text in {
            "1", "true", "yes", "y", "on",
            "disable", "disabled", "expired", "locked",
        }:
            return True
        if text in {
            "0", "false", "no", "n", "off", "normal", "active",
            "enabled", "enabled=0", "not disabled", "not expired",
        }:
            return False
        return None

    @staticmethod
    def _parse_date(value: str) -> date | None:
        text = str(value or "").strip()
        if not text or text.casefold() in {"0", "none", "never", "unlimited", "-", "n/a"}:
            return None

        if text.isdigit():
            number = int(text)
            if number > 1_000_000_000:
                try:
                    return datetime.fromtimestamp(number, tz=timezone.utc).date()
                except (OverflowError, OSError, ValueError):
                    pass

        for fmt in (
            "%Y-%m-%d",
            "%d.%m.%Y",
            "%Y/%m/%d",
            "%d/%m/%Y",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    @classmethod
    def _parse_detail(cls, login: str, output: str) -> SynologyLocalUser:
        fields = cls._detail_fields(output)

        parsed_login = cls._first(
            fields,
            "user name",
            "username",
            "user",
            "name",
        ) or login
        uid = cls._first(fields, "user uid", "uid", "user id")
        email = cls._first(
            fields,
            "user mail",
            "email",
            "mail",
            "e mail",
        ).strip().lower()
        description = cls._first(
            fields,
            "description",
            "full name",
            "fullname",
            "gecos",
        )

        disabled_value = cls._first(
            fields,
            "account disabled",
            "disabled",
            "is disabled",
        )
        expired_value = cls._first(
            fields,
            "account expired",
            "expired",
            "is expired",
        )
        status_text = cls._first(fields, "account status", "status").casefold()

        disabled = cls._as_bool(disabled_value)
        expired = cls._as_bool(expired_value)
        inactive_by_status = any(
            marker in status_text
            for marker in ("disabled", "expired", "locked", "deactivated", "inactive")
        )
        is_active = not (
            disabled is True
            or expired is True
            or inactive_by_status
        )
        status = (
            "disabled"
            if disabled is True or "disabled" in status_text or "deactivated" in status_text
            else "expired"
            if expired is True or "expired" in status_text
            else "locked"
            if "locked" in status_text
            else "active"
            if is_active
            else "inactive"
        )

        expiry_text = cls._first(
            fields,
            "account expiration date",
            "account expire date",
            "expiration date",
            "expire date",
            "expires at",
            "valid until",
        )
        expires_at = cls._parse_date(expiry_text)

        uid_number: int | None = None
        try:
            uid_number = int(uid)
        except (TypeError, ValueError):
            uid_number = None

        lowered_output = str(output or "").casefold()
        protected = (
            parsed_login.casefold() in cls.SYSTEM_LOGINS
            or (uid_number is not None and uid_number < 1024)
            or "administrators" in lowered_output
            or "administrator" in lowered_output and "group" in lowered_output
        )
        stable_id = (
            f"uid:{uid_number}"
            if uid_number is not None
            else f"login:{parsed_login.casefold()}"
        )

        return SynologyLocalUser(
            login=parsed_login,
            stable_id=stable_id,
            uid=str(uid_number) if uid_number is not None else uid,
            email=email,
            description=description,
            status=status,
            is_active=is_active,
            expires_at=expires_at,
            protected=protected,
        )

    def list_accounts(self) -> list[SynologyLocalUser]:
        client = self._client()
        try:
            enum_output = self._execute(client, ["--enum", "local"])
            logins = self._parse_enum_output(enum_output)
            result: list[SynologyLocalUser] = []
            for login in logins:
                try:
                    detail = self._execute(client, ["--get", login])
                    result.append(self._parse_detail(login, detail))
                except Exception as exc:
                    result.append(
                        SynologyLocalUser(
                            login=login,
                            stable_id=f"login:{login.casefold()}",
                            status="unknown",
                            is_active=True,
                            detail_error=str(exc),
                        )
                    )
            return result
        finally:
            client.close()

    def expire_account(self, account: SynologyLocalUser) -> SynologyLocalUser:
        """Отключить локальную учетку установкой Expired=1 и перепроверить DSM.

        `synouser --modify` требует передать также текущее полное имя и e-mail,
        поэтому непосредственно перед изменением карточка перечитывается. UID и
        e-mail должны совпасть с только что классифицированным снимком.
        """
        if not account.login.strip():
            raise ValueError("DSM: пустой login")
        if account.protected or account.login.casefold() in self.SYSTEM_LOGINS:
            raise RuntimeError(f"DSM: защищенная учетка {account.login} не изменяется")
        if not account.email.strip():
            raise RuntimeError(f"DSM: у {account.login} нет e-mail; автоматическая блокировка запрещена")

        client = self._client()
        try:
            before_raw = self._execute(client, ["--get", account.login])
            before = self._parse_detail(account.login, before_raw)

            if before.protected or before.login.casefold() in self.SYSTEM_LOGINS:
                raise RuntimeError(f"DSM: защищенная учетка {before.login} не изменяется")
            if before.stable_id != account.stable_id:
                raise RuntimeError(
                    f"DSM: stable_id изменился для {account.login}: "
                    f"{account.stable_id} -> {before.stable_id}"
                )
            if before.email.strip().lower() != account.email.strip().lower():
                raise RuntimeError(
                    f"DSM: e-mail изменился для {account.login}: "
                    f"{account.email} -> {before.email}"
                )
            if not before.is_active:
                return before

            # Фактическая сигнатура DSM 7.x, проверенная на рабочем NAS:
            # synouser --modify username "full name" expired{0|1} mail
            self._execute(
                client,
                [
                    "--modify",
                    before.login,
                    before.description,
                    "1",
                    before.email,
                ],
            )

            verify_raw = self._execute(client, ["--get", before.login])
            after = self._parse_detail(before.login, verify_raw)
            if after.stable_id != before.stable_id:
                raise RuntimeError(f"DSM: после блокировки изменился stable_id {before.login}")
            if after.is_active:
                raise RuntimeError(f"DSM: {before.login} осталась активной после Expired=1")
            return after
        finally:
            client.close()

    def test_connection(self) -> str:
        """Проверить SSH/synouser без ложного отказа на особой учетке DSM.

        Успешный `--enum local` уже подтверждает доступность интеграции.
        `--get` дополнительно пробуем на нескольких обычных локальных учетках,
        потому что отдельные системные/служебные записи DSM могут перечисляться,
        но не читаться через SYNOUserGet.
        """
        client = self._client()
        try:
            enum_output = self._execute(client, ["--enum", "local"])
            logins = self._parse_enum_output(enum_output)
            candidates = [
                login
                for login in logins
                if login.casefold() not in self.SYSTEM_LOGINS
            ][:10]

            detail_errors = 0
            for login in candidates:
                try:
                    detail = self._execute(client, ["--get", login])
                    parsed = self._parse_detail(login, detail)
                    if parsed.login:
                        return (
                            f"Synology DSM доступен: локальных пользователей {len(logins)}, "
                            f"чтение карточек работает ({parsed.login})."
                        )
                except Exception:
                    detail_errors += 1

            if not logins:
                return "Synology DSM доступен: локальных пользователей не найдено."
            if not candidates:
                return (
                    f"Synology DSM доступен: локальных пользователей {len(logins)}; "
                    "список читается."
                )
            return (
                f"Synology DSM доступен: локальных пользователей {len(logins)}; "
                f"список читается, но тестовые карточки не прочитаны "
                f"({detail_errors} из {len(candidates)})."
            )
        finally:
            client.close()

    def diagnostics(self) -> SynologyDiagnostic:
        """Безопасная диагностика CLI; изменяющие команды не выполняются."""
        client = self._client()
        try:
            help_output = self._execute(client, ["--help"], allow_nonzero=True)
            enum_output = self._execute(client, ["--enum", "local"])
            logins = self._parse_enum_output(enum_output)
            sample_login = ""
            sample_detail = ""
            first_error = ""
            for login in [
                item for item in logins
                if item.casefold() not in self.SYSTEM_LOGINS
            ][:10]:
                try:
                    detail = self._execute(client, ["--get", login])
                    parsed = self._parse_detail(login, detail)
                    if parsed.login:
                        sample_login = login
                        sample_detail = detail
                        break
                except Exception as exc:
                    if not first_error:
                        first_error = f"{login}: {exc}"
            if not sample_detail and first_error:
                sample_detail = first_error
            return SynologyDiagnostic(
                enum_output=enum_output[:12000],
                help_output=help_output[:12000],
                sample_login=sample_login,
                sample_detail=sample_detail[:12000],
            )
        finally:
            client.close()
