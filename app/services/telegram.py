from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models_telegram import TelegramNotification, TelegramSettings


RETRY_DELAYS_MINUTES = (1, 2, 5, 10, 15, 30)
BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
CHAT_ID_RE = re.compile(r"^(?:-?\d+|@[A-Za-z0-9_]{5,})$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        temporary: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.temporary = temporary
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class TelegramSettingsView:
    enabled: bool
    token_configured: bool
    chat_id: str
    topic_id: str
    updated_by: str
    updated_at: datetime | None

    @property
    def configured(self) -> bool:
        return self.token_configured and bool(self.chat_id.strip())


class TelegramSecretBox:
    """Шифрует bot token ключом, производным от APP_SECRET_KEY."""

    def __init__(self, app_secret_key: str):
        secret = str(app_secret_key or "").encode("utf-8")
        if not secret:
            raise RuntimeError("APP_SECRET_KEY не задан")
        digest = hashlib.sha256(secret).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        text = str(value or "")
        if not text:
            return ""
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        text = str(value or "")
        if not text:
            return ""
        try:
            return self._fernet.decrypt(text.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise RuntimeError(
                "Не удалось расшифровать токен Telegram. "
                "Проверьте, что APP_SECRET_KEY не менялся."
            ) from exc


class TelegramBotClient:
    """Минимальный клиент Telegram Bot API без стороннего HTTP-клиента."""

    def __init__(self, token: str, *, timeout_seconds: int = 10):
        self.token = str(token or "").strip()
        self.timeout_seconds = max(1, int(timeout_seconds))
        if not BOT_TOKEN_RE.fullmatch(self.token):
            raise ValueError("Некорректный формат токена Telegram-бота")

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = urllib.parse.urlencode(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "employee-account-manager/telegram",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw = ""
            self._raise_api_error(raw, status_code=exc.code)
            raise AssertionError("unreachable")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramAPIError(
                f"Telegram недоступен: {self._safe_network_error(exc)}",
                temporary=True,
            ) from exc

        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise TelegramAPIError(
                "Telegram вернул некорректный ответ",
                temporary=True,
            ) from exc
        if not data.get("ok"):
            self._raise_api_error(raw, status_code=200)
        result = data.get("result")
        return result if isinstance(result, dict) else {"value": result}

    @staticmethod
    def _safe_network_error(exc: Exception) -> str:
        reason = getattr(exc, "reason", None)
        text = str(reason if reason is not None else exc)
        return text[:500] or exc.__class__.__name__

    @staticmethod
    def _raise_api_error(raw: str, *, status_code: int) -> None:
        description = "Ошибка Telegram Bot API"
        retry_after = None
        error_code = status_code
        try:
            payload = json.loads(raw or "{}")
            description = str(payload.get("description") or description)
            error_code = int(payload.get("error_code") or status_code)
            parameters = payload.get("parameters") or {}
            if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
                retry_after = max(1, int(parameters["retry_after"]))
        except Exception:
            pass

        temporary = bool(error_code == 429 or error_code >= 500)
        raise TelegramAPIError(
            description[:1000],
            temporary=temporary,
            retry_after_seconds=retry_after,
        )

    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        topic_id: str = "",
        parse_mode: str = "",
    ) -> dict:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if topic_id:
            payload["message_thread_id"] = topic_id
        return self._call("sendMessage", payload)


class TelegramService:
    """Web-настройки, тест и надежная очередь Telegram-уведомлений."""

    def __init__(self, app_secret_key: str, db: Session):
        self.db = db
        self.secret_box = TelegramSecretBox(app_secret_key)

    def _record(self) -> TelegramSettings | None:
        return self.db.get(TelegramSettings, 1)

    def view(self) -> TelegramSettingsView:
        record = self._record()
        if record is None:
            return TelegramSettingsView(
                enabled=False,
                token_configured=False,
                chat_id="",
                topic_id="",
                updated_by="",
                updated_at=None,
            )
        return TelegramSettingsView(
            enabled=record.enabled,
            token_configured=bool(record.bot_token_encrypted),
            chat_id=record.chat_id,
            topic_id=record.topic_id,
            updated_by=record.updated_by,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _normalize_chat_id(value: str) -> str:
        chat_id = str(value or "").strip()
        if chat_id and not CHAT_ID_RE.fullmatch(chat_id):
            raise ValueError(
                "Chat ID должен быть числом (например -100...) "
                "или публичным @username"
            )
        return chat_id

    @staticmethod
    def _normalize_topic_id(value: str) -> str:
        topic_id = str(value or "").strip()
        if not topic_id:
            return ""
        if not topic_id.isdigit() or int(topic_id) <= 0:
            raise ValueError("ID темы Telegram должен быть положительным числом")
        return str(int(topic_id))

    @staticmethod
    def _normalize_token(value: str) -> str:
        token = str(value or "").strip()
        if token and not BOT_TOKEN_RE.fullmatch(token):
            raise ValueError("Некорректный формат токена Telegram-бота")
        return token

    def save(
        self,
        *,
        enabled: bool,
        bot_token: str,
        chat_id: str,
        topic_id: str,
        operator: str,
        clear_token: bool = False,
    ) -> TelegramSettingsView:
        record = self._record()
        if record is None:
            record = TelegramSettings(id=1)
            self.db.add(record)

        normalized_token = self._normalize_token(bot_token)
        normalized_chat_id = self._normalize_chat_id(chat_id)
        normalized_topic_id = self._normalize_topic_id(topic_id)

        if clear_token:
            record.bot_token_encrypted = ""
        elif normalized_token:
            record.bot_token_encrypted = self.secret_box.encrypt(normalized_token)

        if enabled and not record.bot_token_encrypted:
            raise ValueError("Для включения Telegram укажите токен бота")
        if enabled and not normalized_chat_id:
            raise ValueError("Для включения Telegram укажите Chat ID")

        record.enabled = bool(enabled)
        record.chat_id = normalized_chat_id
        record.topic_id = normalized_topic_id
        record.updated_by = str(operator or "")[:256]
        record.updated_at = utcnow()
        self.db.commit()
        return self.view()

    def _connection(self) -> tuple[TelegramSettings, TelegramBotClient]:
        record = self._record()
        if record is None or not record.bot_token_encrypted or not record.chat_id.strip():
            raise RuntimeError("Telegram не настроен: нужны токен бота и Chat ID")
        token = self.secret_box.decrypt(record.bot_token_encrypted)
        return record, TelegramBotClient(token)

    def test_connection(self, *, send_test_message: bool = True) -> str:
        record, client = self._connection()
        bot = client.get_me()
        bot_username = str(bot.get("username") or "").strip()
        bot_name = str(bot.get("first_name") or "бот").strip() or "бот"
        bot_label = f"@{bot_username}" if bot_username else bot_name
        if send_test_message:
            client.send_message(
                chat_id=record.chat_id,
                topic_id=record.topic_id,
                text=(
                    "✅ <b>Проверка подключения</b>\n"
                    "Управление учетными записями: Telegram настроен."
                ),
                parse_mode="HTML",
            )
            return f"Подключение работает. Тестовое сообщение отправлено от {bot_label}."
        return f"Telegram Bot API доступен. Бот: {bot_label}."

    def enqueue(
        self,
        text: str,
        *,
        event_type: str = "general",
        dedupe_key: str = "",
        parse_mode: str = "",
    ) -> TelegramNotification | None:
        view = self.view()
        if not view.enabled or not view.configured:
            return None

        message = str(text or "").strip()
        if not message:
            raise ValueError("Нельзя поставить в очередь пустое сообщение Telegram")
        key = str(dedupe_key or "").strip() or None
        mode = str(parse_mode or "").strip()
        if mode not in {"", "HTML"}:
            raise ValueError("Поддерживается только HTML или обычный текст Telegram")
        if key:
            existing = self.db.scalar(
                select(TelegramNotification).where(
                    TelegramNotification.dedupe_key == key
                )
            )
            if existing is not None:
                return existing

        item = TelegramNotification(
            event_type=str(event_type or "general")[:128],
            dedupe_key=key,
            text=message,
            parse_mode=mode,
            status="pending",
            next_attempt_at=utcnow(),
        )
        self.db.add(item)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            if key:
                existing = self.db.scalar(
                    select(TelegramNotification).where(
                        TelegramNotification.dedupe_key == key
                    )
                )
                if existing is not None:
                    return existing
            raise
        self.db.refresh(item)
        return item

    @staticmethod
    def _next_retry(attempts: int) -> datetime:
        index = max(0, min(attempts - 1, len(RETRY_DELAYS_MINUTES) - 1))
        return utcnow() + timedelta(minutes=RETRY_DELAYS_MINUTES[index])

    def _send_item(self, item: TelegramNotification) -> None:
        item.attempts += 1
        try:
            record, client = self._connection()
            result = client.send_message(
                chat_id=record.chat_id,
                topic_id=record.topic_id,
                text=item.text,
                parse_mode=item.parse_mode,
            )
            item.status = "sent"
            item.last_error = ""
            item.next_attempt_at = None
            item.sent_at = utcnow()
            item.telegram_message_id = str(result.get("message_id") or "")[:64]
        except TelegramAPIError as exc:
            item.last_error = str(exc)[:4000]
            item.sent_at = None
            if exc.temporary:
                item.status = "pending"
                if exc.retry_after_seconds:
                    item.next_attempt_at = utcnow() + timedelta(
                        seconds=exc.retry_after_seconds
                    )
                else:
                    item.next_attempt_at = self._next_retry(item.attempts)
            else:
                item.status = "failed"
                item.next_attempt_at = None
        except (RuntimeError, ValueError) as exc:
            item.status = "failed"
            item.last_error = str(exc)[:4000]
            item.sent_at = None
            item.next_attempt_at = None
        except Exception as exc:
            item.status = "pending"
            item.last_error = str(exc)[:4000]
            item.sent_at = None
            item.next_attempt_at = self._next_retry(item.attempts)

    def process_due(self, *, limit: int = 20) -> int:
        view = self.view()
        if not view.enabled or not view.configured:
            return 0
        now = utcnow()
        items = list(
            self.db.scalars(
                select(TelegramNotification)
                .where(
                    TelegramNotification.status == "pending",
                    or_(
                        TelegramNotification.next_attempt_at.is_(None),
                        TelegramNotification.next_attempt_at <= now,
                    ),
                )
                .order_by(TelegramNotification.id)
                .limit(max(1, min(limit, 100)))
            ).all()
        )
        for item in items:
            self._send_item(item)
            self.db.commit()
        return len(items)


    def retry_failed(self, *, limit: int = 100) -> int:
        items = list(
            self.db.scalars(
                select(TelegramNotification)
                .where(TelegramNotification.status == "failed")
                .order_by(TelegramNotification.id)
                .limit(max(1, min(limit, 500)))
            ).all()
        )
        now = utcnow()
        for item in items:
            item.status = "pending"
            item.next_attempt_at = now
        if items:
            self.db.commit()
        return len(items)

    def recent(self, *, limit: int = 20) -> list[TelegramNotification]:
        return list(
            self.db.scalars(
                select(TelegramNotification)
                .order_by(TelegramNotification.id.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
        )
