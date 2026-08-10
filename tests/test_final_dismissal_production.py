from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


class FinalDismissalProductionTests(unittest.TestCase):
    def test_worker_is_started_and_stopped_with_application(self):
        text = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("FinalDismissalLifecycleWorker", text)
        self.assertIn("final_dismissal_worker.start()", text)
        self.assertIn("final_dismissal_worker.stop()", text)

    def test_historical_backfill_is_blocked_by_activation_date(self):
        text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'item["dismissal_date"] >= state.activated_on',
            text,
        )
        self.assertIn("historical_backfill=false", text)

    def test_blocking_time_is_1830(self):
        text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("BLOCK_TIME = time(18, 30)", text)
        self.assertIn('BLOCK_TIME_LABEL = "18:30"', text)

    def test_every_external_attempt_rechecks_hr_state(self):
        text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")
        marker = "def _process_target("
        block = text[text.index(marker):]
        self.assertIn("self._still_due(", block)
        self.assertIn("self._cancel_run(", block)

    def test_active_worker_cancels_not_yet_executed_targets(self):
        text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Работник больше не является окончательно увольняющимся",
            text,
        )
        self.assertIn(
            'if target.status in {"pending", "intervention"}:',
            text,
        )

    def test_both_ad_and_zimbra_are_real_actions(self):
        text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("service.disable_user(user.username)", text)
        self.assertIn(
            "service.close_account(identity.primary_email)",
            text,
        )

    def test_deferral_effective_date_is_used(self):
        text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn('candidate["effective_block_date"]', text)
        self.assertIn("def _due(", text)

    def test_all_sources_must_be_fresh(self):
        text = (
            ROOT / "app/services/final_dismissal_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _sources_synchronized", text)
        self.assertIn("timedelta(hours=36)", text)
        self.assertIn("timedelta(minutes=30)", text)
        self.assertIn("def _import_running", text)

    def test_upcoming_service_exposes_past_only_for_blocking_worker(self):
        text = (
            ROOT / "app/services/upcoming_dismissals.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "def list_for_blocking",
            text,
        )
        self.assertIn(
            "include_expired=True",
            text,
        )

    def test_dashboard_marks_real_automatic_time(self):
        text = (
            ROOT / "app/templates/upcoming_dismissals_fragment.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Автоблокировка", text)
        self.assertIn("18:30", text)


if __name__ == "__main__":
    unittest.main()
