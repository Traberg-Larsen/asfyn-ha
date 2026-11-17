"""Sensors for asfyn meters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AsfynConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AsfynCoordinator, Meter

UNIT = {
    "GJ": (UnitOfEnergy.GIGA_JOULE, SensorDeviceClass.ENERGY),
    "m3": (UnitOfVolume.CUBIC_METERS, SensorDeviceClass.WATER),
    "C": (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
}


@dataclass(frozen=True, kw_only=True)
class AsfynSensorDescription(SensorEntityDescription):
    """Describes one asfyn sensor for a given unit."""

    unit_key: str
    value_fn: Callable[[Meter], Any]
    attrs_fn: Callable[[Meter], dict[str, Any]] | None = None
    last_reset_fn: Callable[[Meter], datetime | None] | None = None


def _round(value: Any, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _descriptions(meter: Meter) -> list[AsfynSensorDescription]:
    out: list[AsfynSensorDescription] = []
    for unit in meter.units:
        u = unit.lower()
        if unit in ("GJ", "m3"):
            out += [
                AsfynSensorDescription(
                    key=f"year_{u}",
                    translation_key=f"year_{u}",
                    unit_key=unit,
                    state_class=SensorStateClass.TOTAL,
                    value_fn=lambda m, unit=unit: _round((m.year.get(unit) or {}).get("total")),
                    last_reset_fn=lambda m: dt_util.start_of_local_day().replace(month=1, day=1),
                    attrs_fn=lambda m, unit=unit: {
                        "til": (m.year.get(unit) or {}).get("endDate"),
                        "lignende_husstande": (m.year.get(unit) or {}).get("benchmarkValue"),
                    },
                ),
                AsfynSensorDescription(
                    key=f"month_{u}",
                    translation_key=f"month_{u}",
                    unit_key=unit,
                    state_class=SensorStateClass.TOTAL,
                    value_fn=lambda m, unit=unit: _round((m.month.get(unit) or {}).get("total")),
                    last_reset_fn=lambda m: dt_util.start_of_local_day().replace(day=1),
                    attrs_fn=lambda m, unit=unit: {
                        "til": (m.month.get(unit) or {}).get("endDate"),
                        "lignende_husstande": (m.month.get(unit) or {}).get("benchmarkValue"),
                    },
                ),
                AsfynSensorDescription(
                    key=f"day_{u}",
                    translation_key=f"day_{u}",
                    unit_key=unit,
                    # energy/water device classes only allow total state classes
                    state_class=None,
                    value_fn=lambda m, unit=unit: _round(m.last_day[unit][1]) if unit in m.last_day else None,
                    attrs_fn=lambda m, unit=unit: {
                        "dato": m.last_day[unit][0].isoformat() if unit in m.last_day else None
                    },
                ),
            ]
        elif unit == "C":
            out += [
                AsfynSensorDescription(
                    key="day_cooling",
                    translation_key="day_cooling",
                    unit_key=unit,
                    state_class=SensorStateClass.MEASUREMENT,
                    value_fn=lambda m: _round(m.last_day["C"][1], 2) if "C" in m.last_day else None,
                    attrs_fn=lambda m: {
                        "dato": m.last_day["C"][0].isoformat() if "C" in m.last_day else None
                    },
                ),
                AsfynSensorDescription(
                    key="month_cooling",
                    translation_key="month_cooling",
                    unit_key=unit,
                    state_class=SensorStateClass.MEASUREMENT,
                    value_fn=lambda m: _round((m.month.get("C") or {}).get("avg"), 2),
                ),
            ]
    if "GJ" in meter.units:
        out.append(
            AsfynSensorDescription(
                key="benchmark",
                translation_key="benchmark",
                unit_key="",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda m: (m.month.get("GJ") or {}).get("benchmarkPerformance"),
            )
        )
    for unit in ("GJ", "m3"):
        if unit in meter.readings:
            out.append(
                AsfynSensorDescription(
                    key=f"reading_{unit.lower()}",
                    translation_key=f"reading_{unit.lower()}",
                    unit_key=unit,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda m, unit=unit: m.readings[unit][0] if unit in m.readings else None,
                    attrs_fn=lambda m, unit=unit: {
                        "aflaest": m.readings[unit][1].isoformat()
                        if unit in m.readings and m.readings[unit][1]
                        else None
                    },
                )
            )
    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AsfynConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        AsfynSensor(coordinator, meter.key, description)
        for meter in coordinator.data.values()
        for description in _descriptions(meter)
    )


class AsfynSensor(CoordinatorEntity[AsfynCoordinator], SensorEntity):
    """A value for one asfyn meter."""

    _attr_has_entity_name = True
    entity_description: AsfynSensorDescription

    def __init__(
        self, coordinator: AsfynCoordinator, meter_key: str, description: AsfynSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._meter_key = meter_key
        meter = coordinator.data[meter_key]
        self._attr_unique_id = f"{meter_key}_{description.key}"
        if description.unit_key in UNIT:
            unit, device_class = UNIT[description.unit_key]
            self._attr_native_unit_of_measurement = unit
            self._attr_device_class = device_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_key)},
            name=f"{meter.name} {meter.meter_number}".strip(),
            manufacturer=MANUFACTURER,
            model=meter.meter_type or meter.category,
            serial_number=meter.meter_number or None,
            suggested_area=None,
        )

    @property
    def _meter(self) -> Meter | None:
        return self.coordinator.data.get(self._meter_key)

    @property
    def available(self) -> bool:
        return super().available and self._meter is not None

    @property
    def native_value(self) -> Any:
        meter = self._meter
        return self.entity_description.value_fn(meter) if meter else None

    @property
    def last_reset(self) -> datetime | None:
        fn = self.entity_description.last_reset_fn
        meter = self._meter
        return fn(meter) if fn and meter else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        meter = self._meter
        return fn(meter) if fn and meter else None
