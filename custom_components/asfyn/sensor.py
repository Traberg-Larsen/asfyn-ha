"""Sensors for asfyn meters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
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
from .prices import Tariff

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


def _latest_bill(meter: Meter) -> dict | None:
    """Newest invoice that is a bill, not a payment."""
    bills = [i for i in meter.invoices if (i.get("amount") or 0) > 0 and i.get("date")]
    return max(bills, key=lambda i: i["date"]) if bills else None


def _bill_attrs(meter: Meter) -> dict[str, Any]:
    bill = _latest_bill(meter)
    if not bill:
        return {}
    return {
        "dato": (bill.get("date") or "")[:10] or None,
        "forfald": (bill.get("dueDate") or "")[:10] or None,
        "type": bill.get("type"),
        "betales_automatisk": bill.get("isHandledByAutomaticPayment"),
    }


def _due_date(meter: Meter) -> date | None:
    bill = _latest_bill(meter)
    due = (bill or {}).get("dueDate")
    return date.fromisoformat(due[:10]) if due else None


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
    out += [
        AsfynSensorDescription(
            key="last_bill",
            translation_key="last_bill",
            unit_key="currency",
            device_class=SensorDeviceClass.MONETARY,
            value_fn=lambda m: (_latest_bill(m) or {}).get("amount"),
            attrs_fn=_bill_attrs,
        ),
        AsfynSensorDescription(
            key="due_date",
            translation_key="due_date",
            unit_key="",
            device_class=SensorDeviceClass.DATE,
            value_fn=_due_date,
        ),
    ]
    return out


@dataclass(frozen=True, kw_only=True)
class TariffSensorDescription(SensorEntityDescription):
    """A Fjernvarme Fyn price from the current price folder."""

    unit_suffix: str
    value_fn: Callable[[Tariff], float | None]


TARIFF_SENSORS = (
    TariffSensorDescription(
        key="price_gj", translation_key="price_gj", unit_suffix="/GJ",
        value_fn=lambda t: t.per_gj,
    ),
    TariffSensorDescription(
        key="price_mwh", translation_key="price_mwh", unit_suffix="/MWh",
        value_fn=lambda t: t.per_mwh,
    ),
    TariffSensorDescription(
        key="price_m3", translation_key="price_m3", unit_suffix="/m³",
        value_fn=lambda t: t.per_m3,
    ),
    TariffSensorDescription(
        key="meter_charge", translation_key="meter_charge", unit_suffix="/år",
        value_fn=lambda t: t.meter_charge,
    ),
    TariffSensorDescription(
        key="effect_charge", translation_key="effect_charge", unit_suffix="/m²/år",
        value_fn=lambda t: t.effect_per_m2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AsfynConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        AsfynSensor(coordinator, meter.key, description)
        for meter in coordinator.data.meters.values()
        for description in _descriptions(meter)
    ]
    entities += [
        TariffSensor(coordinator, meter.key, description)
        for meter in coordinator.data.meters.values()
        if meter.category == "heat"
        for description in TARIFF_SENSORS
    ]
    async_add_entities(entities)


def _device_info(meter: Meter) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, meter.key)},
        name=f"{meter.name} {meter.meter_number}".strip(),
        manufacturer=MANUFACTURER,
        model=meter.meter_type or meter.category,
        serial_number=meter.meter_number or None,
    )


class TariffSensor(CoordinatorEntity[AsfynCoordinator], SensorEntity):
    """Current Fjernvarme Fyn price, incl. VAT."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    entity_description: TariffSensorDescription

    def __init__(
        self, coordinator: AsfynCoordinator, meter_key: str, description: TariffSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{meter_key}_{description.key}"
        self._attr_native_unit_of_measurement = (
            f"{coordinator.hass.config.currency}{description.unit_suffix}"
        )
        self._attr_device_info = _device_info(coordinator.data.meters[meter_key])

    @property
    def native_value(self) -> float | None:
        tariff = self.coordinator.data.tariff_today()
        return self.entity_description.value_fn(tariff) if tariff else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        tariff = self.coordinator.data.tariff_today()
        if not tariff:
            return None
        return {"gaeldende_fra": tariff.valid_from, "kilde": tariff.url, "inkl_moms": True}


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
        meter = coordinator.data.meters[meter_key]
        self._attr_unique_id = f"{meter_key}_{description.key}"
        if description.unit_key in UNIT:
            unit, device_class = UNIT[description.unit_key]
            self._attr_native_unit_of_measurement = unit
            self._attr_device_class = device_class
        elif description.unit_key == "currency":
            self._attr_native_unit_of_measurement = coordinator.hass.config.currency
        self._attr_device_info = _device_info(meter)

    @property
    def _meter(self) -> Meter | None:
        return self.coordinator.data.meters.get(self._meter_key)

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
