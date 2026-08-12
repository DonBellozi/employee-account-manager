from __future__ import annotations

import logging
import threading

from app.config import get_settings
from app.services.telegram import TelegramService
from app.services.telegram_zimbra_daily_report import TelegramZimbraDailyReportService


logger = logging.getLogger(__name__)


class TelegramNotificationWorker:
    """Фоновая доставка сообщений и постановка утреннего Zimbra-отчета."""

    def __init__(
        self,
        app_secret_key: str,
        session_factory,
        *,
        poll_seconds: int = 10,
    ) -> None:
        self.app_secret_key = app_secret_key
        self.session_factory = session_factory
        self.poll_seconds = max(2, int(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="telegram-notification-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2, self.poll_seconds + 1))
        self._thread = None

    def _run(self) -> None:
        settings = get_settings()
        while not self._stop.is_set():
            try:
                with self.session_factory() as db:
                    TelegramZimbraDailyReportService(
                        settings,
                        db,
                    ).enqueue_due()
                    TelegramService(
                        self.app_secret_key,
                        db,
                    ).process_due()
            except Exception:
                logger.exception("Telegram notification worker failed")
            self._stop.wait(self.poll_seconds)
