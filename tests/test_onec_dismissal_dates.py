from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from app.services.onec_xlsx import parse_onec_xlsx


class OneCDismissalDateTests(unittest.TestCase):
    def _xlsx(self, rows) -> Path:
        wb = Workbook()
        ws = wb.active
        ws.append([
            "СНИЛС",
            "Сотрудник",
            "Должность",
            "Физическое лицо.Адрес электронной почты",
            "Дата увольнения",
        ])
        for row in rows:
            ws.append(row)
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        path = Path(handle.name)
        wb.save(path)
        wb.close()
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_one_active_position_keeps_worker_active(self):
        path = self._xlsx([
            ["123-456-789 01", "Иванов Иван", "Инженер", "ivanov@example.ru", "15.08.2026"],
            ["123-456-789 01", "Иванов Иван", "Менеджер", "ivanov@example.ru", ""],
        ])
        book = parse_onec_xlsx(path, hash_secret="secret", header_search_rows=5)
        self.assertEqual(book.dismissal_column, "Дата увольнения")
        self.assertEqual(len(book.workers), 1)
        self.assertIsNone(book.workers[0].dismissal_date)

    def test_all_positions_dismissed_use_last_date(self):
        path = self._xlsx([
            ["123-456-789 01", "Иванов Иван", "Инженер", "ivanov@example.ru", "15.08.2026"],
            ["123-456-789 01", "Иванов Иван", "Менеджер", "ivanov@example.ru", "20.08.2026"],
        ])
        book = parse_onec_xlsx(path, hash_secret="secret", header_search_rows=5)
        self.assertEqual(book.workers[0].dismissal_date, date(2026, 8, 20))

    def test_dismissal_date_is_part_of_snapshot(self):
        from app.services.onec_xlsx import worker_snapshot

        path = self._xlsx([
            ["123-456-789 01", "Иванов Иван", "Инженер", "ivanov@example.ru", "20.08.2026"],
        ])
        book = parse_onec_xlsx(path, hash_secret="secret", header_search_rows=5)
        self.assertEqual(worker_snapshot(book.workers[0])["dismissal_date"], "2026-08-20")


if __name__ == "__main__":
    unittest.main()
