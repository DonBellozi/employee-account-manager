from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings


@dataclass(frozen=True)
class ITInventEquipment:
    equipment_type: str
    equipment_name: str
    serial_number: str
    inventory_number: str
    accounting_inventory_number: str


@dataclass(frozen=True)
class ITInventEmployeeAssets:
    owner_found: bool
    owner_display_name: str
    owner_login: str
    equipment: tuple[ITInventEquipment, ...]


@dataclass(frozen=True)
class _ModelLookup:
    select_sql: str
    join_sql: str = ""


class ITInventService:
    """Read-only доступ к IT Invent на MS SQL Server.

    Подтвержденные связи БД:
      OWNERS.OWNER_LOGIN -> доменный sAMAccountName
      ITEMS.EMPL_NO = OWNERS.OWNER_NO
      ITEMS.LOC_NO = 24 -> «Выданы в пользование»
      ITEMS.TYPE_NO/CI_TYPE -> CI_TYPES.TYPE_NO/CI_TYPE

    Название конкретной техники в интерфейсе IT Invent соответствует модели.
    Так как публичной схемы таблицы моделей нет, сервис безопасно определяет
    связь ITEMS -> справочник моделей через INFORMATION_SCHEMA и использует
    только SELECT-запросы.

    В сервисе намеренно отсутствуют любые методы INSERT/UPDATE/DELETE.
    """

    _TEXT_TYPES = {
        "char",
        "nchar",
        "varchar",
        "nvarchar",
        "text",
        "ntext",
    }
    _IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")

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
        database_name = (
            str(row[0] or self.settings.itinvent_db_name)
            if row
            else self.settings.itinvent_db_name
        )
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

    @classmethod
    def _quote_identifier(cls, value: str) -> str:
        if not cls._IDENTIFIER_RE.fullmatch(value):
            raise ValueError("Некорректный SQL-идентификатор в схеме IT Invent")
        return f"[{value}]"

    @classmethod
    def _discover_model_lookup(cls, cursor) -> _ModelLookup:
        """Определить, где IT Invent хранит название модели техники.

        Схема разных выпусков IT Invent может отличаться, поэтому вместо
        жестко заданного имени таблицы читаем INFORMATION_SCHEMA. При любой
        неоднозначности возвращаем пустое название, не ломая выдачу техники.
        """
        try:
            cursor.execute(
                """
                SELECT
                    TABLE_NAME,
                    COLUMN_NAME,
                    DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE
                    TABLE_SCHEMA = 'dbo'
                    AND (
                        UPPER(TABLE_NAME) = 'ITEMS'
                        OR UPPER(TABLE_NAME) LIKE '%MODEL%'
                    )
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """
            )
            rows = cursor.fetchall()
        except Exception:
            return _ModelLookup(
                select_sql="CAST(N'' AS nvarchar(255)) AS EquipmentName"
            )

        tables: dict[str, dict[str, tuple[str, str]]] = {}
        actual_table_names: dict[str, str] = {}
        for row in rows:
            if not row or len(row) < 3:
                continue
            table = str(row[0] or "").strip()
            column = str(row[1] or "").strip()
            data_type = str(row[2] or "").strip().lower()
            if not table or not column:
                continue
            if not cls._IDENTIFIER_RE.fullmatch(table):
                continue
            if not cls._IDENTIFIER_RE.fullmatch(column):
                continue
            table_key = table.upper()
            actual_table_names[table_key] = table
            tables.setdefault(table_key, {})[column.upper()] = (
                column,
                data_type,
            )

        item_columns = tables.get("ITEMS", {})
        if not item_columns:
            return _ModelLookup(
                select_sql="CAST(N'' AS nvarchar(255)) AS EquipmentName"
            )

        # В некоторых схемах название модели может храниться прямо в ITEMS.
        for candidate in ("MODEL_NAME", "MODEL", "NAME"):
            value = item_columns.get(candidate)
            if value is None:
                continue
            actual, data_type = value
            if data_type in cls._TEXT_TYPES:
                return _ModelLookup(
                    select_sql=(
                        "NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), "
                        f"i.{cls._quote_identifier(actual)}))), N'') "
                        "AS EquipmentName"
                    )
                )

        item_ref_key = ""
        for candidate in ("MODEL_NO", "MODEL_ID", "MODEL"):
            if candidate in item_columns:
                item_ref_key = candidate
                break
        if not item_ref_key:
            return _ModelLookup(
                select_sql="CAST(N'' AS nvarchar(255)) AS EquipmentName"
            )

        item_ref_actual = item_columns[item_ref_key][0]
        candidates: list[tuple[int, str, str, str]] = []
        for table_key, columns in tables.items():
            if table_key == "ITEMS" or "MODEL" not in table_key:
                continue

            key_key = ""
            for candidate in (
                item_ref_key,
                "MODEL_NO",
                "MODEL_ID",
                "ID",
            ):
                if candidate in columns:
                    key_key = candidate
                    break
            if not key_key:
                continue

            name_key = ""
            for candidate in (
                "MODEL_NAME",
                "NAME",
                "DESCR",
                "DESCRIPTION",
                "MODEL",
            ):
                value = columns.get(candidate)
                if value is not None and value[1] in cls._TEXT_TYPES:
                    name_key = candidate
                    break
            if not name_key:
                continue

            score = 0
            if table_key == "MODELS":
                score += 100
            elif table_key == "CI_MODELS":
                score += 90
            else:
                score += 50
            if key_key == item_ref_key:
                score += 20
            if name_key == "MODEL_NAME":
                score += 20
            candidates.append((score, table_key, key_key, name_key))

        if not candidates:
            return _ModelLookup(
                select_sql="CAST(N'' AS nvarchar(255)) AS EquipmentName"
            )

        candidates.sort(reverse=True)
        _, table_key, key_key, name_key = candidates[0]
        model_columns = tables[table_key]
        table_actual = actual_table_names[table_key]
        key_actual = model_columns[key_key][0]
        name_actual = model_columns[name_key][0]

        join_parts = [
            f"m.{cls._quote_identifier(key_actual)} = "
            f"i.{cls._quote_identifier(item_ref_actual)}"
        ]
        # Модель в IT Invent зависит от вида и типа. Если эти поля есть в
        # обеих таблицах, включаем их в JOIN, чтобы не получить чужую модель
        # с тем же внутренним номером.
        for extra_key in ("TYPE_NO", "CI_TYPE"):
            item_value = item_columns.get(extra_key)
            model_value = model_columns.get(extra_key)
            if item_value is not None and model_value is not None:
                join_parts.append(
                    f"m.{cls._quote_identifier(model_value[0])} = "
                    f"i.{cls._quote_identifier(item_value[0])}"
                )

        join_sql = (
            f"LEFT JOIN dbo.{cls._quote_identifier(table_actual)} m ON "
            + " AND ".join(join_parts)
        )
        select_sql = (
            "NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), "
            f"m.{cls._quote_identifier(name_actual)}))), N'') "
            "AS EquipmentName"
        )
        return _ModelLookup(select_sql=select_sql, join_sql=join_sql)

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
            model_lookup = self._discover_model_lookup(cursor)
            cursor.execute(
                f"""
                SELECT
                    ct.TYPE_NAME AS EquipmentType,
                    {model_lookup.select_sql},
                    i.SERIAL_NO AS SerialNumber,
                    i.INV_NO AS InventoryNumber,
                    i.INV_NO_BUH AS AccountingInventoryNumber
                FROM dbo.ITEMS i
                LEFT JOIN dbo.CI_TYPES ct
                    ON ct.TYPE_NO = i.TYPE_NO
                   AND ct.CI_TYPE = i.CI_TYPE
                {model_lookup.join_sql}
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
                equipment_name=str(row[1] or "").strip(),
                serial_number=str(row[2] or "").strip(),
                inventory_number=self.identifier_text(row[3]),
                accounting_inventory_number=str(row[4] or "").strip(),
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
