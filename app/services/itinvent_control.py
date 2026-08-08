from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ITInventControlSettings
from app.services.itinvent import (
    ITInventEquipmentType,
    ITInventLocation,
    ITInventService,
)


@dataclass(frozen=True)
class ITInventControlSelection:
    locations: tuple[ITInventLocation, ...]
    equipment_types: tuple[ITInventEquipmentType, ...]
    persisted: bool

    @property
    def location_nos(self) -> tuple[str, ...]:
        return tuple(item.loc_no for item in self.locations)

    @property
    def equipment_type_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (item.type_no, item.ci_type)
            for item in self.equipment_types
        )


class ITInventControlService:
    """Настройки, определяющие какое имущество требует внимания.

    Правило едино для ручной карточки «Блокировка» и будущей автоматической
    обработки увольнений: закрепленная за сотрудником учетная единица считается
    контролируемой, если ее тип выбран как «всегда учитывать» ИЛИ ее
    местоположение входит в выбранный список.
    """

    SETTINGS_ID = 1

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @staticmethod
    def type_key(type_no: str, ci_type: str) -> str:
        return f"{str(type_no).strip()}|{str(ci_type).strip()}"

    def _fallback(self) -> ITInventControlSelection:
        loc_no = str(self.settings.itinvent_issued_location_no).strip()
        label = (
            "Выданы в пользование"
            if loc_no == "24"
            else f"LOC_NO {loc_no}"
        )
        return ITInventControlSelection(
            locations=(
                ITInventLocation(loc_no=loc_no, description=label),
            ) if loc_no else (),
            equipment_types=(),
            persisted=False,
        )

    @staticmethod
    def _parse_locations(raw: str) -> tuple[ITInventLocation, ...]:
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return ()
        result: list[ITInventLocation] = []
        if not isinstance(data, list):
            return ()
        for item in data:
            if not isinstance(item, dict):
                continue
            loc_no = str(item.get("loc_no") or "").strip()
            description = str(item.get("description") or "").strip()
            if loc_no:
                result.append(
                    ITInventLocation(
                        loc_no=loc_no,
                        description=description or f"LOC_NO {loc_no}",
                    )
                )
        return tuple(result)

    @staticmethod
    def _parse_types(raw: str) -> tuple[ITInventEquipmentType, ...]:
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return ()
        result: list[ITInventEquipmentType] = []
        if not isinstance(data, list):
            return ()
        for item in data:
            if not isinstance(item, dict):
                continue
            type_no = str(item.get("type_no") or "").strip()
            ci_type = str(item.get("ci_type") or "").strip()
            type_name = str(item.get("type_name") or "").strip()
            if type_no and ci_type:
                result.append(
                    ITInventEquipmentType(
                        type_no=type_no,
                        ci_type=ci_type,
                        type_name=type_name or f"TYPE_NO {type_no}",
                    )
                )
        return tuple(result)

    def load(self) -> ITInventControlSelection:
        row = self.db.get(ITInventControlSettings, self.SETTINGS_ID)
        if row is None:
            return self._fallback()
        return ITInventControlSelection(
            locations=self._parse_locations(row.locations_json),
            equipment_types=self._parse_types(row.equipment_types_json),
            persisted=True,
        )

    def summary(self) -> dict[str, object]:
        selection = self.load()
        return {
            "locations": [item.description for item in selection.locations],
            "types": [item.type_name for item in selection.equipment_types],
            "locations_count": len(selection.locations),
            "types_count": len(selection.equipment_types),
            "persisted": selection.persisted,
        }

    def catalog_payload(self) -> dict[str, object]:
        itinvent = ITInventService(self.settings)
        locations = itinvent.list_locations()
        equipment_types = itinvent.list_equipment_types()
        selection = self.load()
        return {
            "locations": [
                {
                    "key": item.loc_no,
                    "loc_no": item.loc_no,
                    "name": item.description,
                }
                for item in locations
            ],
            "types": [
                {
                    "key": self.type_key(item.type_no, item.ci_type),
                    "type_no": item.type_no,
                    "ci_type": item.ci_type,
                    "name": item.type_name,
                }
                for item in equipment_types
            ],
            "selected_locations": list(selection.location_nos),
            "selected_types": [
                self.type_key(type_no, ci_type)
                for type_no, ci_type in selection.equipment_type_keys
            ],
        }

    def save_from_keys(
        self,
        *,
        location_keys: list[str],
        type_keys: list[str],
        operator: str,
    ) -> ITInventControlSelection:
        itinvent = ITInventService(self.settings)
        locations = itinvent.list_locations()
        equipment_types = itinvent.list_equipment_types()

        location_by_key = {item.loc_no: item for item in locations}
        type_by_key = {
            self.type_key(item.type_no, item.ci_type): item
            for item in equipment_types
        }

        normalized_locations = list(
            dict.fromkeys(str(value).strip() for value in location_keys if str(value).strip())
        )
        normalized_types = list(
            dict.fromkeys(str(value).strip() for value in type_keys if str(value).strip())
        )

        unknown_locations = [
            value for value in normalized_locations if value not in location_by_key
        ]
        unknown_types = [
            value for value in normalized_types if value not in type_by_key
        ]
        if unknown_locations or unknown_types:
            raise ValueError(
                "Справочники IT Invent изменились. Обновите список и повторите сохранение."
            )

        selected_locations = tuple(
            location_by_key[value] for value in normalized_locations
        )
        selected_types = tuple(
            type_by_key[value] for value in normalized_types
        )

        row = self.db.get(ITInventControlSettings, self.SETTINGS_ID)
        if row is None:
            row = ITInventControlSettings(id=self.SETTINGS_ID)
            self.db.add(row)

        row.locations_json = json.dumps(
            [
                {
                    "loc_no": item.loc_no,
                    "description": item.description,
                }
                for item in selected_locations
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        row.equipment_types_json = json.dumps(
            [
                {
                    "type_no": item.type_no,
                    "ci_type": item.ci_type,
                    "type_name": item.type_name,
                }
                for item in selected_types
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        row.updated_by = operator
        self.db.commit()
        self.db.refresh(row)

        return ITInventControlSelection(
            locations=selected_locations,
            equipment_types=selected_types,
            persisted=True,
        )
