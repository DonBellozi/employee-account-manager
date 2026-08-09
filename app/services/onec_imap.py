from __future__ import annotations

import email
import hashlib
import imaplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header

from app.config import Settings


@dataclass(frozen=True)
class OneCAttachment:
    uid: str
    message_date: str
    sender: str
    subject: str
    filename: str
    file_hash: str
    payload: bytes


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    result: list[str] = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


class OneCImapService:
    """Read-only доступ к кадровым выгрузкам 1С."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _validate_connection(self) -> None:
        missing = []
        if not self.settings.onec_imap_host:
            missing.append("ONEC_IMAP_HOST")
        if not self.settings.onec_imap_username:
            missing.append("ONEC_IMAP_USERNAME")
        if not self.settings.onec_imap_password:
            missing.append("ONEC_IMAP_PASSWORD")
        if missing:
            raise RuntimeError(
                "Не заполнены настройки IMAP для 1С: " + ", ".join(missing)
            )

    def _connect(self):
        self._validate_connection()
        client_cls = (
            imaplib.IMAP4_SSL
            if self.settings.onec_imap_ssl
            else imaplib.IMAP4
        )
        client = client_cls(
            self.settings.onec_imap_host,
            self.settings.onec_imap_port,
        )
        client.login(
            self.settings.onec_imap_username,
            self.settings.onec_imap_password,
        )
        return client

    def test_connection(self) -> str:
        with self._connect() as imap:
            status, _ = imap.select(
                self.settings.onec_imap_folder,
                readonly=True,
            )
            if status != "OK":
                raise RuntimeError(
                    f"Не удалось открыть папку "
                    f"{self.settings.onec_imap_folder} в режиме readonly"
                )
        return "IMAP-подключение работает."

    def find_latest_attachment(
        self,
        *,
        folder: str | None = None,
        sender_filter: str | None = None,
        attachment_filename: str | None = None,
    ) -> OneCAttachment:
        sender = (
            self.settings.onec_imap_from_contains
            if sender_filter is None
            else sender_filter
        ).strip()
        expected = (
            self.settings.onec_attachment_filename
            if attachment_filename is None
            else attachment_filename
        ).strip()
        selected_folder = (
            self.settings.onec_imap_folder
            if folder is None
            else folder
        ).strip() or "INBOX"
        if not expected:
            raise RuntimeError("Не задано имя вложения кадровой выгрузки")

        with self._connect() as imap:
            status, _ = imap.select(
                selected_folder,
                readonly=True,
            )
            if status != "OK":
                raise RuntimeError(
                    f"Не удалось открыть папку "
                    f"{selected_folder} в режиме readonly"
                )

            since = (
                datetime.now()
                - timedelta(
                    days=max(1, self.settings.onec_imap_lookback_days)
                )
            ).strftime("%d-%b-%Y")

            criteria: list[str] = ["SINCE", since]
            if sender:
                criteria.extend(["FROM", f'"{sender}"'])

            status, data = imap.uid("search", None, *criteria)
            if status != "OK":
                raise RuntimeError("Ошибка поиска письма по IMAP")

            uids = data[0].split() if data and data[0] else []
            if not uids:
                raise FileNotFoundError("Подходящие письма не найдены")

            for uid_bytes in reversed(uids):
                uid = uid_bytes.decode()
                status, message_data = imap.uid(
                    "fetch",
                    uid,
                    "(BODY.PEEK[])",
                )
                if status != "OK" or not message_data:
                    continue

                raw = None
                for item in message_data:
                    if isinstance(item, tuple) and isinstance(item[1], bytes):
                        raw = item[1]
                        break
                if raw is None:
                    continue

                message = email.message_from_bytes(raw)
                message_sender = decode_mime(message.get("From"))
                subject = decode_mime(message.get("Subject"))
                message_date = decode_mime(message.get("Date"))

                for part in message.walk():
                    filename = decode_mime(part.get_filename())
                    if filename != expected:
                        continue
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    return OneCAttachment(
                        uid=uid,
                        message_date=message_date,
                        sender=message_sender,
                        subject=subject,
                        filename=filename,
                        file_hash=hashlib.sha256(payload).hexdigest(),
                        payload=payload,
                    )

        raise FileNotFoundError(
            f"В найденных письмах нет вложения {expected}"
        )
