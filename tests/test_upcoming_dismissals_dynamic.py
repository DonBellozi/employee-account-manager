from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpcomingDismissalsDynamicTests(unittest.TestCase):
    def test_fragment_endpoint_exists(self):
        text = (
            ROOT / "app/routers/dashboard_dismissals.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '@router.get("/dismissals/upcoming/fragment")',
            text,
        )
        self.assertIn(
            '"upcoming_dismissals_fragment.html"',
            text,
        )

    def test_defer_keeps_regular_form_fallback_and_ajax_response(self):
        text = (
            ROOT / "app/routers/dashboard_dismissals.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"application/json" in request.headers.get("accept", "")',
            text,
        )
        self.assertIn("RedirectResponse", text)
        self.assertIn("JSONResponse", text)

    def test_dashboard_uses_replaceable_fragment(self):
        text = (
            ROOT / "app/templates/dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="upcoming-dismissals-dynamic"', text)
        self.assertIn(
            '{% include "upcoming_dismissals_fragment.html" %}',
            text,
        )
        self.assertIn('/static/upcoming_dismissals.js', text)

    def test_fragment_marks_defer_form_for_ajax(self):
        text = (
            ROOT / "app/templates/upcoming_dismissals_fragment.html"
        ).read_text(encoding="utf-8")
        self.assertIn("data-upcoming-dismissal-defer", text)
        self.assertIn('name="csrf"', text)
        self.assertIn('name="worker_key"', text)
        self.assertIn('name="dismissal_date"', text)

    def test_script_polls_without_reloading_page(self):
        text = (
            ROOT / "app/static/upcoming_dismissals.js"
        ).read_text(encoding="utf-8")
        self.assertIn("refreshIntervalMs = 30000", text)
        self.assertIn("visibilitychange", text)
        self.assertIn("new FormData(form)", text)
        self.assertIn("host.innerHTML = html", text)
        self.assertNotIn("window.location.reload", text)
        self.assertNotIn("location.href", text)


if __name__ == "__main__":
    unittest.main()
