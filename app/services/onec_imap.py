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


@dataclass(frozen=True)
class OneCImapScanResult:
    """Результат инкрементального просмотра IMAP.

    max_uid можно безопасно сохранить как курсор только после того, как найденная
    выгрузка была успешно принята либо признана точным дубликатом текущего снимка.
    """

    max_uid: str
    attachment: OneCAttachment | None


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

    def _filters(
        self,
        *,
        folder: str | None,
        sender_filter: str | None,
        attachment_filename: str | None,
    ) -> tuple[str, str, str]:
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
        return selected_folder, sender, expected

    @staticmethod
    def _uid_number(value: str | bytes | None) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def _fetch_raw(self, imap, uid: str) -> bytes:
        status, message_data = imap.uid(
            "fetch",
            uid,
            "(BODY.PEEK[])",
        )
        if status != "OK" or not message_data:
            raise RuntimeError(f"Не удалось прочитать письмо IMAP UID {uid}")

        for item in message_data:
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                return item[1]
        raise RuntimeError(f"Письмо IMAP UID {uid} получено без содержимого")

    @staticmethod
    def _attachment_from_raw(
        *,
        uid: str,
        raw: bytes,
        expected_filename: str,
    ) -> OneCAttachment | None:
        message = email.message_from_bytes(raw)
        message_sender = decode_mime(message.get("From"))
        subject = decode_mime(message.get("Subject"))
        message_date = decode_mime(message.get("Date"))

        for part in message.walk():
            filename = decode_mime(part.get_filename())
            if filename != expected_filename:
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
        return None

    def scan_newest_attachment(
        self,
        *,
        after_uid: str = "",
        folder: str | None = None,
        sender_filter: str | None = None,
        attachment_filename: str | None = None,
    ) -> OneCImapScanResult:
        """Просматривает только письма новее after_uid и возвращает самый новый XLSX.

        Если среди новых писем нужного вложения нет, attachment=None, а max_uid
        позволяет сдвинуть курсор и больше не перечитывать эти письма.
        """

        selected_folder, sender, expected = self._filters(
            folder=folder,
            sender_filter=sender_filter,
            attachment_filename=attachment_filename,
        )
        after_number = self._uid_number(after_uid)

        with self._connect() as imap:
            status, _ = imap.select(selected_folder, readonly=True)
            if status != "OK":
                raise RuntimeError(
                    f"Не удалось открыть папку {selected_folder} в режиме readonly"
                )

            since = (
                datetime.now()
                - timedelta(days=max(1, self.settings.onec_imap_lookback_days))
            ).strftime("%d-%b-%Y")

            criteria: list[str] = []
            if after_number:
                criteria.extend(["UID", f"{after_number + 1}:*"])
            criteria.extend(["SINCE", since])
            if sender:
                criteria.extend(["FROM", f'"{sender}"'])

            status, data = imap.uid("search", None, *criteria)
            if status != "OK":
                raise RuntimeError("Ошибка поиска письма по IMAP")

            uid_values = data[0].split() if data and data[0] else []
            # Некоторые IMAP-серверы трактуют диапазон N:* необычно, если N
            # уже больше текущего максимального UID. Отсекаем старые UID сами.
            uid_values = [
                value
                for value in uid_values
                if self._uid_number(value) > after_number
            ]
            if not uid_values:
                return OneCImapScanResult(
                    max_uid=str(after_number) if after_number else "",
                    attachment=None,
                )

            numeric_uids = [self._uid_number(value) for value in uid_values]
            max_uid = max(numeric_uids)

            # Идем с конца: полный XLSX является снимком, поэтому при накопившихся
            # нескольких выгрузках достаточно самого нового корректного письма.
            for uid_bytes in reversed(uid_values):
                uid = uid_bytes.decode()
                raw = self._fetch_raw(imap, uid)
                attachment = self._attachment_from_raw(
                    uid=uid,
                    raw=raw,
                    expected_filename=expected,
                )
                if attachment is not None:
                    return OneCImapScanResult(
                        max_uid=str(max_uid),
                        attachment=attachment,
                    )

            return OneCImapScanResult(
                max_uid=str(max_uid),
                attachment=None,
            )

    def find_latest_attachment(
        self,
        *,
        folder: str | None = None,
        sender_filter: str | None = None,
        attachment_filename: str | None = None,
    ) -> OneCAttachment:
        result = self.scan_newest_attachment(
            folder=folder,
            sender_filter=sender_filter,
            attachment_filename=attachment_filename,
        )
        if result.attachment is not None:
            return result.attachment

        _, _, expected = self._filters(
            folder=folder,
            sender_filter=sender_filter,
            attachment_filename=attachment_filename,
        )
        raise FileNotFoundError(
            f"В найденных письмах нет вложения {expected}"
        )
