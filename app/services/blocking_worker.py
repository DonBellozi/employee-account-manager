from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.services.blocking_queue import BlockingQueueService

logger = logging.getLogger(__name__)


class BlockingQueueWorker:
    """Фоново завершает отложенные блокировки AD/Zimbra."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        *,
        poll_seconds: float = 10.0,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.poll_seconds = max(2.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="blocking-queue-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.poll_seconds + 2.0)
        self._thread = None

    def _run(self) -> None:
        # Проверяем сразу после старта, чтобы задания переживали рестарт приложения.
        while not self._stop.is_set():
            try:
                with self.session_factory() as db:
                    BlockingQueueService(self.settings, db).process_due()
            except Exception:
                logger.exception("Ошибка фоновой обработки очереди блокировок")
            if self._stop.wait(self.poll_seconds):
                break
