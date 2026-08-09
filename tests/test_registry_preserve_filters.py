from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RegistryPreserveFiltersTests(unittest.TestCase):
    def test_router_builds_return_to_with_query_string(self):
        text = (
            ROOT / "app/routers/hr_registry_multisource.py"
        ).read_text(encoding="utf-8")
        self.assertIn("current_query = request.url.query", text)
        self.assertIn('return_to += f"?{current_query}"', text)
        self.assertIn('"return_to": return_to', text)

    def test_checked_form_uses_current_return_to(self):
        text = (
            ROOT / "app/templates/hr_registry.html"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'name="return_to" value="{{ return_to }}"',
            text,
        )
        self.assertNotIn(
            'name="return_to" value="/employees/registry"',
            text,
        )


if __name__ == "__main__":
    unittest.main()
