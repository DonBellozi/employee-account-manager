from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings


@dataclass(frozen=True)
class ITInventEquipment:
    equipment_type: str
    serial_number: str
    inventory_number: str
    accounting_inventory_number: str


@dataclass(frozen=True)
class ITInventEmployeeAssets:
    owner_found: bool
    owner_display_name: str
    owner_login: str
    equipment: tuple[ITInventEquipment, ...]


class ITInventService:
    """Read-only доступ к IT Invent на MS SQL Server.

    Подтвержденные связи БД:
      OWNERS.OWNER_LOGIN -> доменный sAMAccountName
      ITEMS.EMPL_NO = OWNERS.OWNER_NO
      ITEMS.LOC_NO = 24 -> «Выданы в пользование»
      ITEMS.TYPE_NO/CI_TYPE -> CI_TYPES.TYPE_NO/CI_TYPE

    В сервисе намеренно отсутствуют любые методы INSERT/UPDATE/DELETE.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.itinvent_db_host.strip()
            and self.settings.itinvent_db_name.strip()
            and self.settings.itinvent_db_username.strip()
            and self.settings.itinvent_db_password
        )

    def _validate(self) -> None:
        missing: list[str] = []
        if not self.settings.itinvent_db_host.strip():
            missing.append("ITINVENT_DB_HOST")
        if not self.settings.itinvent_db_name.strip():
            missing.append("ITINVENT_DB_NAME")
        if not self.settings.itinvent_db_username.strip():
            missing.append("ITINVENT_DB_USERNAME")
        if not self.settings.itinvent_db_password:
            missing.append("ITINVENT_DB_PASSWORD")
        if missing:
            raise RuntimeError(
                "Не заполнены настройки IT Invent: " + ", ".join(missing)
            )

    @staticmethod
    def _driver():
        try:
            import pymssql  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Не установлен драйвер pymssql для подключения к IT Invent"
            ) from exc
        return pymssql

    def _connect(self):
        self._validate()
        pymssql = self._driver()
        return pymssql.connect(
            server=self.settings.itinvent_db_host.strip(),
            port=str(self.settings.itinvent_db_port),
            user=self.settings.itinvent_db_username.strip(),
            password=self.settings.itinvent_db_password,
            database=self.settings.itinvent_db_name.strip(),
            login_timeout=max(1, self.settings.itinvent_connect_timeout_seconds),
            timeout=max(1, self.settings.itinvent_query_timeout_seconds),
            charset="UTF-8",
            autocommit=True,
        )

    def test_connection(self) -> str:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DB_NAME(), "
                "(SELECT COUNT_BIG(*) FROM dbo.OWNERS)"
            )
            row = cursor.fetchone()
        database_name = str(row[0] or self.settings.itinvent_db_name) if row else self.settings.itinvent_db_name
        owners_count = int(row[1] or 0) if row and len(row) > 1 else 0
        return (
            "Подключение к IT Invent работает в режиме чтения. "
            f"База: {database_name}; записей OWNERS: {owners_count}."
        )

    @staticmethod
    def identifier_text(value: object) -> str:
        """Отобразить идентификатор без лишнего `.0` у SQL float.

        INV_NO в IT Invent имеет тип float, хотя по смыслу является
        идентификатором, а не числом для расчетов.
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, Decimal):
            text = format(value, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return text
        if isinstance(value, float):
            if not math.isfinite(value):
                return str(value)
            if value.is_integer():
                return str(int(value))
            return format(value, ".15g")
        text = str(value).strip()
        if re_full_float_zero(text):
            return text[:-2]
        return text

    def equipment_for_login(self, login: str) -> ITInventEmployeeAssets:
        normalized = str(login or "").strip().lower()
        if not normalized:
            raise ValueError("Не передан доменный логин для поиска в IT Invent")

        with self._connect() as conn:
            cursor = conn.cursor()
            # Сначала разрешаем владельца отдельно, чтобы отличать ситуацию
            # «сотрудник найден, имущества нет» от «сотрудник не найден».
            cursor.execute(
                """
                SELECT TOP (2)
                    OWNER_NO,
                    OWNER_DISPLAY_NAME,
                    OWNER_LOGIN
                FROM dbo.OWNERS
                WHERE LOWER(LTRIM(RTRIM(OWNER_LOGIN))) = LOWER(%s)
                ORDER BY OWNER_NO
                """,
                (normalized,),
            )
            owners = cursor.fetchall()
            if not owners:
                return ITInventEmployeeAssets(
                    owner_found=False,
                    owner_display_name="",
                    owner_login=normalized,
                    equipment=(),
                )
            if len(owners) > 1:
                raise RuntimeError(
                    "В IT Invent найдено несколько сотрудников с одним "
                    f"доменным логином {normalized}"
                )

            owner_no, owner_name, owner_login = owners[0]
            cursor.execute(
                """
                SELECT
                    ct.TYPE_NAME AS EquipmentType,
                    i.SERIAL_NO AS SerialNumber,
                    i.INV_NO AS InventoryNumber,
                    i.INV_NO_BUH AS AccountingInventoryNumber
                FROM dbo.ITEMS i
                LEFT JOIN dbo.CI_TYPES ct
                    ON ct.TYPE_NO = i.TYPE_NO
                   AND ct.CI_TYPE = i.CI_TYPE
                WHERE
                    i.EMPL_NO = %s
                    AND i.LOC_NO = %s
                ORDER BY
                    ct.TYPE_NAME,
                    i.INV_NO
                """,
                (
                    owner_no,
                    self.settings.itinvent_issued_location_no,
                ),
            )
            rows = cursor.fetchall()

        equipment = tuple(
            ITInventEquipment(
                equipment_type=str(row[0] or "").strip(),
                serial_number=str(row[1] or "").strip(),
                inventory_number=self.identifier_text(row[2]),
                accounting_inventory_number=str(row[3] or "").strip(),
            )
            for row in rows
        )
        return ITInventEmployeeAssets(
            owner_found=True,
            owner_display_name=str(owner_name or "").strip(),
            owner_login=str(owner_login or normalized).strip().lower(),
            equipment=equipment,
        )


def re_full_float_zero(value: str) -> bool:
    if not value.endswith(".0"):
        return False
    head = value[:-2]
    return bool(head) and head.lstrip("+-").isdigit()
