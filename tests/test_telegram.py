from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Изолированный тест сервиса: полный checkout проекта для этих тестов не нужен.
class _Base(DeclarativeBase):
    pass


db_stub = types.ModuleType("app.db")
db_stub.Base = _Base
sys.modules["app.db"] = db_stub

from app.models_telegram import TelegramNotification, TelegramSettings  # noqa: E402
from app.services.telegram import (  # noqa: E402
    TelegramAPIError,
    TelegramService,
)


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        _Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = TelegramService("strong-app-secret", self.db)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_token_is_encrypted_and_blank_value_preserves_it(self):
        token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234567890"
        self.service.save(
            enabled=True,
            bot_token=token,
            chat_id="-1001234567890",
            topic_id="",
            operator="ivanov.ii",
        )
        record = self.db.get(TelegramSettings, 1)
        self.assertTrue(record.bot_token_encrypted)
        self.assertNotEqual(record.bot_token_encrypted, token)
        self.assertNotIn(token, record.bot_token_encrypted)

        encrypted = record.bot_token_encrypted
        self.service.save(
            enabled=True,
            bot_token="",
            chat_id="-1001234567890",
            topic_id="42",
            operator="ivanov.ii",
        )
        self.db.refresh(record)
        self.assertEqual(record.bot_token_encrypted, encrypted)
        self.assertEqual(record.topic_id, "42")

    def test_enqueue_deduplicates_by_key(self):
        self.service.save(
            enabled=True,
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234567890",
            chat_id="-1001234567890",
            topic_id="",
            operator="operator",
        )
        first = self.service.enqueue(
            "<b>Отчет</b>",
            event_type="test",
            dedupe_key="job:42",
        )
        second = self.service.enqueue(
            "Другой текст",
            event_type="test",
            dedupe_key="job:42",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            self.db.query(TelegramNotification).count(),
            1,
        )

    @patch("app.services.telegram.TelegramBotClient")
    def test_temporary_error_stays_pending(self, client_cls):
        self.service.save(
            enabled=True,
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234567890",
            chat_id="-1001234567890",
            topic_id="",
            operator="operator",
        )
        item = self.service.enqueue("Сообщение", event_type="test")
        client_cls.return_value.send_message.side_effect = TelegramAPIError(
            "Too Many Requests",
            temporary=True,
            retry_after_seconds=5,
        )
        processed = self.service.process_due()
        self.assertEqual(processed, 1)
        self.db.refresh(item)
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.attempts, 1)
        self.assertIsNotNone(item.next_attempt_at)

    @patch("app.services.telegram.TelegramBotClient")
    def test_permanent_error_can_be_returned_to_queue(self, client_cls):
        self.service.save(
            enabled=True,
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234567890",
            chat_id="-1001234567890",
            topic_id="",
            operator="operator",
        )
        item = self.service.enqueue("Сообщение", event_type="test")
        client_cls.return_value.send_message.side_effect = TelegramAPIError(
            "Bad Request: chat not found",
            temporary=False,
        )
        self.service.process_due()
        self.db.refresh(item)
        self.assertEqual(item.status, "failed")
        self.assertIsNone(item.next_attempt_at)

        self.assertEqual(self.service.retry_failed(), 1)
        self.db.refresh(item)
        self.assertEqual(item.status, "pending")
        self.assertIsNotNone(item.next_attempt_at)

    @patch("app.services.telegram.TelegramBotClient")
    def test_success_marks_message_sent(self, client_cls):
        self.service.save(
            enabled=True,
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234567890",
            chat_id="-1001234567890",
            topic_id="17",
            operator="operator",
        )
        item = self.service.enqueue("Сообщение", event_type="test")
        client_cls.return_value.send_message.return_value = {"message_id": 777}
        self.service.process_due()
        self.db.refresh(item)
        self.assertEqual(item.status, "sent")
        self.assertEqual(item.telegram_message_id, "777")
        self.assertIsNotNone(item.sent_at)


if __name__ == "__main__":
    unittest.main()
