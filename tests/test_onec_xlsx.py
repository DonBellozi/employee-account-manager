import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.services.onec_xlsx import parse_onec_xlsx, worker_snapshot


class OneCXlsxTests(unittest.TestCase):
    def make(self, state, personal_email="ivan.personal@example.net"):
        wb = Workbook()
        ws = wb.active
        ws.append(["Отчет"])
        ws.append(
            [
                "СНИЛС",
                "Сотрудник.Физическое лицо.ФИО",
                "Физическое лицо.Адрес электронной почты",
                "Физическое лицо.Email",
                "Должность",
                "Состояние",
            ]
        )
        ws.append(["Отдел ИТ"])
        ws.append(
            [
                "123-456-789 01",
                "Иванов Иван Иванович",
                "ivanov.ii@example.ru",
                personal_email,
                "Специалист",
                state,
            ]
        )
        file = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        path = Path(file.name)
        file.close()
        wb.save(path)
        wb.close()
        return path

    def test_state_ignored_and_personal_email_parsed(self):
        a = self.make("Работа")
        b = self.make("Отпуск")
        try:
            ra = parse_onec_xlsx(a, hash_secret="0123456789abcdef")
            rb = parse_onec_xlsx(b, hash_secret="0123456789abcdef")
        finally:
            a.unlink(missing_ok=True)
            b.unlink(missing_ok=True)

        self.assertNotIn("state", ra.detected_columns)
        self.assertIn("personal_email", ra.detected_columns)
        self.assertEqual(ra.workers[0].personal_email, "ivan.personal@example.net")
        self.assertEqual(worker_snapshot(ra.workers[0]), worker_snapshot(rb.workers[0]))
        self.assertNotEqual(ra.workers[0].worker_key, "12345678901")

    def test_invalid_personal_email_is_ignored(self):
        path = self.make("Работа", personal_email="not-an-email")
        try:
            parsed = parse_onec_xlsx(path, hash_secret="0123456789abcdef")
        finally:
            path.unlink(missing_ok=True)
        self.assertIsNone(parsed.workers[0].personal_email)


if __name__ == "__main__":
    unittest.main()
