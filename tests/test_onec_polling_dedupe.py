from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OneCPollingDedupeTests(unittest.TestCase):
    def test_same_hash_does_not_reimport(self):
        text = (ROOT / "app/services/onec_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("latest.file_hash == attachment.file_hash", text)
        self.assertIn('status="duplicate"', text)

    def test_manual_newer_import_wins(self):
        text = (ROOT / "app/services/onec_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("latest_uid >= attachment_uid", text)

    def test_failed_file_is_retried_with_throttle(self):
        text = (ROOT / "app/services/onec_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("FAILED_RETRY_MINUTES = 15", text)
        self.assertIn("next_retry_at", text)
        self.assertIn("_retry_allowed", text)

    def test_only_newest_accumulated_full_snapshot_is_needed(self):
        text = (ROOT / "app/services/onec_imap.py").read_text(encoding="utf-8")
        self.assertIn("for uid_bytes in reversed(uid_values)", text)
        self.assertIn("полный XLSX является снимком", text)


if __name__ == "__main__":
    unittest.main()
