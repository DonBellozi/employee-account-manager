from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.services.onec_xlsx import parse_onec_xlsx


ROOT = Path(__file__).resolve().parents[1]


class OneCImapPollingSafetyTests(unittest.TestCase):
    def test_poll_interval_is_five_minutes(self):
        text = (ROOT / "app/services/onec_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("POLL_INTERVAL_SECONDS = 5 * 60", text)
        self.assertIn('name="onec-imap-polling"', text)

    def test_incremental_uid_cursor_is_used(self):
        imap_text = (ROOT / "app/services/onec_imap.py").read_text(encoding="utf-8")
        scheduler_text = (ROOT / "app/services/onec_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("after_uid", imap_text)
        self.assertIn('criteria.extend(["UID", f"{after_number + 1}:*"])', imap_text)
        self.assertIn("last_scanned_uid", scheduler_text)
        self.assertIn("scan_newest_attachment", scheduler_text)

    def test_imap_is_read_only_and_never_marks_messages(self):
        text = (ROOT / "app/services/onec_imap.py").read_text(encoding="utf-8")
        self.assertIn("readonly=True", text)
        self.assertIn('"(BODY.PEEK[])"', text)
        self.assertNotIn('"STORE"', text)
        self.assertNotIn("\\Deleted", text)

    def test_control_export_is_required_before_blocking(self):
        freshness = (ROOT / "app/services/onec_freshness.py").read_text(encoding="utf-8")
        lifecycle = (ROOT / "app/services/final_dismissal_lifecycle.py").read_text(encoding="utf-8")
        window = (ROOT / "app/services/blocking_window.py").read_text(encoding="utf-8")
        self.assertIn("CONTROL_EXPORT_TIME = time(19, 0)", freshness)
        self.assertIn("all_control_exports_ready", lifecycle)
        self.assertIn("BLOCK_TIME = time(19, 10)", window)
        self.assertIn("from app.services.blocking_window import", lifecycle)

    def test_suspicious_mass_drop_is_rejected_before_import(self):
        text = (ROOT / "app/services/onec_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("current_count * 2 < previous_count", text)
        self.assertIn("Кадровое состояние не изменено", text)

    def test_source_config_change_resets_imap_cursor(self):
        text = (ROOT / "app/services/onec_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("_source_config_key", text)
        self.assertIn('row.last_scanned_uid = ""', text)
        self.assertIn('row.last_status = "reset"', text)

    def test_settings_ui_describes_polling_and_control_gate(self):
        text = (ROOT / "app/static/settings_extensions.js").read_text(encoding="utf-8")
        self.assertIn("каждые 5 минут, круглосуточно", text)
        self.assertIn("после 19:00; автоблокировка не ранее 19:10", text)
        self.assertIn("IMAP только в режиме read-only", text)


class OneCXlsxFailClosedTests(unittest.TestCase):
    def _save(self, rows: list[list[object]]) -> Path:
        book = Workbook()
        sheet = book.active
        for row in rows:
            sheet.append(row)
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        path = Path(handle.name)
        book.save(path)
        book.close()
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_empty_snapshot_is_rejected(self):
        path = self._save([
            ["СНИЛС", "Сотрудник", "Должность", "Дата увольнения"],
        ])
        with self.assertRaisesRegex(ValueError, "не содержит ни одного работника"):
            parse_onec_xlsx(path, hash_secret="test-secret")

    def test_snapshot_without_dismissal_column_is_rejected(self):
        path = self._save([
            ["СНИЛС", "Сотрудник", "Должность"],
            ["123-456-789 01", "Иванов Иван Иванович", "Инженер"],
        ])
        with self.assertRaisesRegex(ValueError, "Дата увольнения"):
            parse_onec_xlsx(path, hash_secret="test-secret")

    def test_valid_snapshot_with_dismissal_column_is_accepted(self):
        path = self._save([
            ["СНИЛС", "Сотрудник", "Должность", "Дата увольнения"],
            ["123-456-789 01", "Иванов Иван Иванович", "Инженер", "10.08.2026"],
        ])
        result = parse_onec_xlsx(path, hash_secret="test-secret")
        self.assertEqual(len(result.workers), 1)
        self.assertEqual(result.dismissal_column, "Дата увольнения")
        self.assertEqual(result.workers[0].dismissal_date.isoformat(), "2026-08-10")


if __name__ == "__main__":
    unittest.main()
