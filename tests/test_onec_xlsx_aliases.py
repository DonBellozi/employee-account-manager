from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.services.onec_xlsx import parse_onec_xlsx


HEADERS_NEW = [
    "СНИЛС",
    "Сотрудник",
    "Должность",
    "Дата рождения",
    "Состояние",
    "Физическое   лицо.Email",
    "Физическое   лицо.Адрес электронной почты",
    "Стаж работы на   предприятии лет",
    "Пол",
    "Дата увольнения",
]


class OneCXlsxHeaderAliasesTests(unittest.TestCase):
    def _parse(self, headers):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "source.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.append(headers)
            sheet.append([
                "123-456-789 01",
                "Иванов Иван Иванович",
                "Инженер",
                "01.01.1990",
                "Работает",
                "private@example.net",
                "ivanov@company.ru",
                "5",
                "М",
                None,
            ])
            book.save(path)
            book.close()
            return parse_onec_xlsx(
                path,
                hash_secret="test-secret",
            )

    def test_new_employee_header_is_supported(self):
        parsed = self._parse(HEADERS_NEW)
        self.assertEqual(len(parsed.workers), 1)
        worker = parsed.workers[0]
        self.assertEqual(worker.fio, "Иванов Иван Иванович")
        self.assertEqual(worker.email, "ivanov@company.ru")
        self.assertEqual(worker.personal_email, "private@example.net")
        self.assertEqual(worker.login, "ivanov")
        self.assertEqual(worker.placements[0].position, "Инженер")
        self.assertIn("Дата увольнения", parsed.potential_dismissal_columns)

    def test_old_employee_header_remains_supported(self):
        headers = list(HEADERS_NEW)
        headers[1] = "Сотрудник.Физическое лицо.ФИО"
        parsed = self._parse(headers)
        self.assertEqual(len(parsed.workers), 1)
        self.assertEqual(parsed.workers[0].fio, "Иванов Иван Иванович")

    def test_short_alias_does_not_match_other_employee_field(self):
        headers = list(HEADERS_NEW)
        headers[1] = "Сотрудник.Скрыть день рождения (Сотрудники)"
        with self.assertRaisesRegex(ValueError, "fio"):
            self._parse(headers)


if __name__ == "__main__":
    unittest.main()
