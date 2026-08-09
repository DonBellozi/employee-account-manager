from __future__ import annotations

import os
import re
import shlex
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models_zimbra_lifecycle import (
    ZimbraLifecycleAction,
    ZimbraLifecycleRun,
    ZimbraLifecycleSettings,
)
from app.models_zimbra_observer import ZimbraLifecycleState
from app.models_zimbra_protection import ZimbraProtectedAccount
from app.services.zimbra import ZimbraService
from app.services.zimbra_protection import ManagedZimbraObserverService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BackupResult:
    path: str
    size_bytes: int


class ZimbraLifecycleService:
    """Планирование и поэтапное ручное исполнение жизненного цикла Zimbra.

    Источник решений – ManagedZimbraObserverService. Этот класс не содержит
    собственной логики сроков 6/12 месяцев и не обходит исключения 1С/Web.
    Перед каждым планом и исполнением запускается свежая read-only проверка.
    """

    _execution_lock = threading.Lock()
    ACTIONABLE_RECOMMENDATIONS = ("close", "archive_delete")

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def get_settings_record(self) -> ZimbraLifecycleSettings:
        row = self.db.get(ZimbraLifecycleSettings, 1)
        if row is None:
            row = ZimbraLifecycleSettings(
                id=1,
                allow_close=False,
                allow_backup=False,
                allow_delete=False,
                backup_dir="/app/data/zimbra-backups",
            )
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    @staticmethod
    def _normalize_backup_dir(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Укажите каталог резервных копий на сервере Zimbra")
        if "\x00" in text or "\n" in text or "\r" in text:
            raise ValueError("Каталог резервных копий содержит недопустимые символы")
        path = PurePosixPath(text)
        if not path.is_absolute():
            raise ValueError("Каталог резервных копий должен быть абсолютным путем")
        if ".." in path.parts:
            raise ValueError("Каталог резервных копий не должен содержать '..'")
        normalized = str(path)
        persistent_root = "/app/data"
        if normalized != persistent_root and not normalized.startswith(persistent_root + "/"):
            raise ValueError(
                "Резервные копии должны храниться внутри /app/data, "
                "который подключен как постоянный Docker volume"
            )
        return normalized.rstrip("/")

    def settings_view(self) -> dict[str, object]:
        row = self.get_settings_record()
        return {
            "allow_close": bool(row.allow_close),
            "allow_backup": bool(row.allow_backup),
            "allow_delete": bool(row.allow_delete),
            "backup_dir": row.backup_dir,
            "updated_by": row.updated_by,
            "updated_at": row.updated_at,
            "global_dry_run": bool(self.settings.dry_run),
            "any_action_enabled": bool(
                row.allow_close or row.allow_backup or row.allow_delete
            ),
        }

    def save_settings(
        self,
        *,
        allow_close: bool,
        allow_backup: bool,
        allow_delete: bool,
        backup_dir: str,
        operator: str,
    ) -> ZimbraLifecycleSettings:
        if allow_delete and not allow_backup:
            raise ValueError(
                "Удаление нельзя разрешить без резервного копирования"
            )
        normalized_dir = self._normalize_backup_dir(backup_dir)
        row = self.get_settings_record()
        row.allow_close = bool(allow_close)
        row.allow_backup = bool(allow_backup)
        row.allow_delete = bool(allow_delete)
        row.backup_dir = normalized_dir
        row.updated_by = str(operator or "")[:256]
        row.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    def recent_runs(self, limit: int = 20) -> list[ZimbraLifecycleRun]:
        return list(
            self.db.scalars(
                select(ZimbraLifecycleRun)
                .order_by(desc(ZimbraLifecycleRun.started_at), desc(ZimbraLifecycleRun.id))
                .limit(max(1, min(int(limit), 100)))
            ).all()
        )

    def get_run(self, run_id: int) -> ZimbraLifecycleRun | None:
        return self.db.get(ZimbraLifecycleRun, int(run_id))

    def run_actions(self, run_id: int) -> list[ZimbraLifecycleAction]:
        return list(
            self.db.scalars(
                select(ZimbraLifecycleAction)
                .where(ZimbraLifecycleAction.run_id == int(run_id))
                .order_by(ZimbraLifecycleAction.id)
            ).all()
        )

    def _start_run(self, mode: str, operator: str) -> ZimbraLifecycleRun:
        row = ZimbraLifecycleRun(
            mode=mode,
            trigger="manual",
            status="running",
            operator=str(operator or "")[:256],
            started_at=utcnow(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _actionable_states(self) -> list[ZimbraLifecycleState]:
        return list(
            self.db.scalars(
                select(ZimbraLifecycleState)
                .where(
                    ZimbraLifecycleState.recommendation.in_(
                        self.ACTIONABLE_RECOMMENDATIONS
                    )
                )
                .order_by(
                    ZimbraLifecycleState.recommendation,
                    ZimbraLifecycleState.primary_email,
                )
            ).all()
        )

    def _fresh_states(self, run: ZimbraLifecycleRun) -> list[ZimbraLifecycleState]:
        observer_run = ManagedZimbraObserverService(self.settings, self.db).run(
            trigger="manual"
        )
        run.observation_run_id = observer_run.id
        self.db.commit()
        if observer_run.status == "failed":
            raise RuntimeError(
                observer_run.error_message
                or "Свежая проверка Zimbra завершилась ошибкой"
            )
        return self._actionable_states()

    def _add_action(
        self,
        run: ZimbraLifecycleRun,
        state: ZimbraLifecycleState,
        *,
        action: str,
        status: str,
        message: str,
        backup_path: str = "",
        backup_size_bytes: int | None = None,
    ) -> ZimbraLifecycleAction:
        row = ZimbraLifecycleAction(
            run_id=run.id,
            account_key=state.account_key,
            zimbra_id=state.zimbra_id,
            primary_email=state.primary_email,
            recommendation=state.recommendation,
            action=action,
            status=status,
            message=message,
            backup_path=backup_path,
            backup_size_bytes=backup_size_bytes,
            created_at=utcnow(),
            completed_at=utcnow() if status != "planned" else None,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def build_plan(self, operator: str) -> ZimbraLifecycleRun:
        run = self._start_run("plan", operator)
        try:
            states = self._fresh_states(run)
            run.planned_close = sum(s.recommendation == "close" for s in states)
            run.planned_archive = sum(
                s.recommendation == "archive_delete" for s in states
            )
            for state in states:
                if state.recommendation == "close":
                    message = (
                        "DRY RUN: учетная запись будет закрыта, если разрешение "
                        "закрытия включено на момент исполнения."
                    )
                    action = "close"
                else:
                    message = (
                        "DRY RUN: для закрытой учетной записи потребуется создать "
                        "непустой TGZ backup. Удаление возможно только после успешного "
                        "backup и при отдельном разрешении удаления."
                    )
                    action = "backup_delete"
                self._add_action(
                    run,
                    state,
                    action=action,
                    status="planned",
                    message=message,
                )
            run.status = "success"
            run.completed_at = utcnow()
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            run = self.db.get(ZimbraLifecycleRun, run.id)
            if run is not None:
                run.status = "failed"
                run.error_message = str(exc)
                run.completed_at = utcnow()
                self.db.commit()
                self.db.refresh(run)
                return run
            raise

    def _is_web_protected(self, zimbra_id: str) -> bool:
        if not str(zimbra_id or "").strip():
            return False
        return (
            self.db.scalar(
                select(ZimbraProtectedAccount.id).where(
                    ZimbraProtectedAccount.zimbra_id == zimbra_id.strip(),
                    ZimbraProtectedAccount.is_active.is_(True),
                )
            )
            is not None
        )

    def _current_status(self, email: str) -> str | None:
        """Читает фактический статус прямо из Zimbra; None означает NO_SUCH_ACCOUNT."""
        zimbra = ZimbraService(self.settings)
        if self.settings.zimbra_backend == "disabled":
            raise RuntimeError("Zimbra backend отключен")
        client = zimbra._client()
        try:
            try:
                output = zimbra._execute_zmprov_lookup(
                    client,
                    ["ga", email, "zimbraAccountStatus", "zimbraId"],
                )
            except RuntimeError as exc:
                if zimbra._is_not_found_error(exc):
                    return None
                raise
        finally:
            client.close()

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("zimbraaccountstatus:"):
                return line.split(":", 1)[1].strip().lower()
        return ""

    def _backup_account(
        self,
        email: str,
        *,
        backup_dir: str,
        run_id: int,
    ) -> BackupResult:
        """Потоково сохраняет TGZ из zmmailbox в persistent volume приложения.

        На Zimbra-сервере не используется shell-редирект и не требуется широкое
        sudo на /bin/sh. SSH-пользователю достаточно права запускать конкретный
        /opt/zimbra/bin/zmmailbox от имени zimbra.
        """
        if self.settings.dry_run:
            raise RuntimeError("APP DRY_RUN запрещает создание резервной копии")

        directory_text = self._normalize_backup_dir(backup_dir)
        directory = Path(directory_text)
        directory.mkdir(parents=True, exist_ok=True)

        safe_email = re.sub(r"[^A-Za-z0-9._@+-]+", "_", email)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        filename = f"{safe_email}-run{run_id}-{stamp}.tgz"
        final_path = directory / filename
        temp_path = directory / (filename + ".part")

        if temp_path.exists():
            temp_path.unlink()

        env_and_binary = (
            "/usr/bin/env LC_ALL=ru_RU.utf8 LANG=ru_RU.utf8 "
            "/opt/zimbra/bin/zmmailbox"
        )
        args = ["-z", "-m", email, "getRestURL", "//?fmt=tgz"]
        if self.settings.zimbra_ssh_user.strip().lower() == "zimbra":
            command = f"{env_and_binary} {shlex.join(args)}"
        else:
            command = (
                "sudo -n -u zimbra "
                f"{env_and_binary} {shlex.join(args)}"
            )

        zimbra = ZimbraService(self.settings)
        client = zimbra._client()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=3600)
            stdin.channel.shutdown_write()
            channel = stdout.channel
            deadline = time.monotonic() + 3600.0
            err_chunks: list[bytes] = []

            try:
                with temp_path.open("wb") as backup_file:
                    while True:
                        progressed = False
                        while channel.recv_ready():
                            backup_file.write(channel.recv(1024 * 1024))
                            progressed = True
                        while channel.recv_stderr_ready():
                            err_chunks.append(channel.recv_stderr(65536))
                            progressed = True

                        if (
                            channel.exit_status_ready()
                            and not channel.recv_ready()
                            and not channel.recv_stderr_ready()
                        ):
                            break
                        if time.monotonic() >= deadline:
                            channel.close()
                            raise RuntimeError(
                                f"Превышено время создания резервной копии {email}"
                            )
                        if not progressed:
                            time.sleep(0.05)

                code = channel.recv_exit_status()
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
        finally:
            client.close()

        err = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
        if code != 0:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(
                "zmmailbox не смог создать резервную копию: "
                f"{err or f'код {code}'}"
            )

        size = temp_path.stat().st_size if temp_path.exists() else 0
        if size <= 0:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Резервная копия {email} имеет нулевой размер; удаление запрещено"
            )

        os.replace(temp_path, final_path)
        return BackupResult(path=str(final_path), size_bytes=size)

    def _finish_run(self, run: ZimbraLifecycleRun) -> ZimbraLifecycleRun:
        if run.failed_count:
            run.status = "partial" if (
                run.closed_success
                or run.backup_success
                or run.delete_success
                or run.skipped_count
            ) else "failed"
        else:
            run.status = "success"
        run.completed_at = utcnow()
        self.db.commit()
        self.db.refresh(run)
        return run

    def execute(self, operator: str) -> ZimbraLifecycleRun:
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError("Исполнение жизненного цикла Zimbra уже выполняется")

        run = self._start_run("execute", operator)
        try:
            config = self.get_settings_record()
            states = self._fresh_states(run)
            run.planned_close = sum(s.recommendation == "close" for s in states)
            run.planned_archive = sum(
                s.recommendation == "archive_delete" for s in states
            )
            self.db.commit()

            for state in states:
                # Последний локальный предохранитель: Web-исключение могло быть
                # включено уже после свежего наблюдения, но до фактического шага.
                if self._is_web_protected(state.zimbra_id):
                    self._add_action(
                        run,
                        state,
                        action="skip",
                        status="blocked",
                        message=(
                            "Действие отменено: перед исполнением обнаружена "
                            "активная Web-защита учетной записи."
                        ),
                    )
                    run.skipped_count += 1
                    self.db.commit()
                    continue

                if self.settings.dry_run:
                    self._add_action(
                        run,
                        state,
                        action=(
                            "close"
                            if state.recommendation == "close"
                            else "backup_delete"
                        ),
                        status="planned",
                        message=(
                            "APP DRY_RUN включен: никаких изменений в Zimbra "
                            "не выполнялось."
                        ),
                    )
                    run.skipped_count += 1
                    self.db.commit()
                    continue

                if state.recommendation == "close":
                    self._execute_close(run, state, config)
                elif state.recommendation == "archive_delete":
                    self._execute_archive(run, state, config)

            return self._finish_run(run)
        except Exception as exc:
            self.db.rollback()
            current = self.db.get(ZimbraLifecycleRun, run.id)
            if current is not None:
                current.status = "failed"
                current.error_message = str(exc)
                current.completed_at = utcnow()
                self.db.commit()
                self.db.refresh(current)
                return current
            raise
        finally:
            self._execution_lock.release()

    def _execute_close(
        self,
        run: ZimbraLifecycleRun,
        state: ZimbraLifecycleState,
        config: ZimbraLifecycleSettings,
    ) -> None:
        if not config.allow_close:
            self._add_action(
                run,
                state,
                action="close",
                status="blocked",
                message="Закрытие не разрешено в Web-настройках исполнителя.",
            )
            run.skipped_count += 1
            self.db.commit()
            return

        try:
            status = self._current_status(state.primary_email)
            if status is None:
                raise RuntimeError(
                    "Учетная запись больше не найдена в Zimbra перед закрытием"
                )
            if status == "closed":
                self._add_action(
                    run,
                    state,
                    action="close",
                    status="success",
                    message="На момент выполнения уже была закрыта.",
                )
                run.closed_success += 1
                self.db.commit()
                return
            if status != "active":
                raise RuntimeError(
                    f"Ожидался статус active, фактический статус – {status or 'не указан'}"
                )

            ZimbraService(self.settings).close_account(state.primary_email)
            verified = self._current_status(state.primary_email)
            if verified != "closed":
                raise RuntimeError(
                    "Команда закрытия выполнена, но статус closed не подтвержден"
                )
            self._add_action(
                run,
                state,
                action="close",
                status="success",
                message="Учетная запись Zimbra закрыта; статус closed подтвержден.",
            )
            run.closed_success += 1
        except Exception as exc:
            self._add_action(
                run,
                state,
                action="close",
                status="failed",
                message=str(exc),
            )
            run.failed_count += 1
        self.db.commit()

    def _execute_archive(
        self,
        run: ZimbraLifecycleRun,
        state: ZimbraLifecycleState,
        config: ZimbraLifecycleSettings,
    ) -> None:
        if not config.allow_backup:
            self._add_action(
                run,
                state,
                action="backup",
                status="blocked",
                message=(
                    "Резервное копирование не разрешено. Удаление без нового "
                    "успешного backup невозможно."
                ),
            )
            run.skipped_count += 1
            self.db.commit()
            return

        try:
            status = self._current_status(state.primary_email)
            if status is None:
                raise RuntimeError(
                    "Учетная запись больше не найдена в Zimbra перед backup"
                )
            if status != "closed":
                raise RuntimeError(
                    f"Backup+удаление разрешены только для closed; фактический статус – "
                    f"{status or 'не указан'}"
                )

            backup = self._backup_account(
                state.primary_email,
                backup_dir=config.backup_dir,
                run_id=run.id,
            )
            self._add_action(
                run,
                state,
                action="backup",
                status="success",
                message="TGZ backup создан и ненулевой размер подтвержден.",
                backup_path=backup.path,
                backup_size_bytes=backup.size_bytes,
            )
            run.backup_success += 1
            self.db.commit()
        except Exception as exc:
            self._add_action(
                run,
                state,
                action="backup",
                status="failed",
                message=str(exc),
            )
            run.failed_count += 1
            self.db.commit()
            return

        if not config.allow_delete:
            self._add_action(
                run,
                state,
                action="delete",
                status="blocked",
                message=(
                    "Backup успешно создан, но удаление не разрешено в Web-настройках."
                ),
                backup_path=backup.path,
                backup_size_bytes=backup.size_bytes,
            )
            run.skipped_count += 1
            self.db.commit()
            return

        try:
            # Повторная проверка защиты прямо перед необратимым шагом.
            if self._is_web_protected(state.zimbra_id):
                raise RuntimeError(
                    "Удаление отменено: после backup включена Web-защита учетной записи"
                )
            status = self._current_status(state.primary_email)
            if status != "closed":
                raise RuntimeError(
                    "Удаление отменено: непосредственно перед удалением "
                    "статус closed не подтвержден"
                )

            ZimbraService(self.settings).delete_account(state.primary_email)
            if self._current_status(state.primary_email) is not None:
                raise RuntimeError(
                    "Команда удаления выполнена, но учетная запись все еще находится в Zimbra"
                )
            self._add_action(
                run,
                state,
                action="delete",
                status="success",
                message=(
                    "Учетная запись удалена после успешного backup; отсутствие "
                    "учетной записи в Zimbra подтверждено."
                ),
                backup_path=backup.path,
                backup_size_bytes=backup.size_bytes,
            )
            run.delete_success += 1
        except Exception as exc:
            self._add_action(
                run,
                state,
                action="delete",
                status="failed",
                message=str(exc),
                backup_path=backup.path,
                backup_size_bytes=backup.size_bytes,
            )
            run.failed_count += 1
        self.db.commit()
