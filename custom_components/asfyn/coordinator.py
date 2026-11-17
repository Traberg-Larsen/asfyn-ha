"""Data coordinator for asfyn: polls the API and imports daily statistics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AsfynAuthError, AsfynClient, AsfynError
from .const import CATEGORY_UNITS, CONF_CUSTOMERS, DOMAIN, STATISTIC_UNITS, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

UNIT_LABELS = {"GJ": "GJ", "m3": "m³", "C": "°C"}
UNIT_CLASS = {"GJ": "energy", "m3": "volume"}


@dataclass
class Meter:
    """One meter (heat, water or sewer) and its latest data."""

    customer: str
    estate_id: int
    installation: str
    meter: str
    category: str
    name: str
    meter_number: str
    meter_type: str
    address: str
    filters: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    readings: dict[str, tuple[float, date | None]] = field(default_factory=dict)
    year: dict[str, dict] = field(default_factory=dict)
    month: dict[str, dict] = field(default_factory=dict)
    last_day: dict[str, tuple[date, float]] = field(default_factory=dict)
    month_cache: dict[tuple[str, int, int], dict] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.customer}_{self.installation}_{self.meter}"

    @property
    def units(self) -> list[str]:
        if "month" not in self.filters:
            return []
        return CATEGORY_UNITS.get(self.category, [])


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def daily_values(response: dict, today: date) -> list[tuple[date, float, bool]]:
    """(day, value, estimated) for the days in a month response that have data.

    Future days come back as 0; summary.endDate marks where real data stops.
    """
    summary = response.get("summary") or {}
    end = _parse_date(summary.get("endDate"))
    if end is None:
        return []
    out = []
    for item in response.get("items") or []:
        day = _parse_date(item.get("date"))
        value = item.get("value")
        if day is None or value is None or day >= today or day > end:
            continue
        if day == end and not value:
            continue
        out.append((day, float(value), bool(item.get("isEstimatedMeasurement"))))
    return out


class AsfynCoordinator(DataUpdateCoordinator[dict[str, Meter]]):
    """Polls asfyn every few hours."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: AsfynClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self._stats_lock = asyncio.Lock()

    async def _async_update_data(self) -> dict[str, Meter]:
        try:
            meters = await self._fetch()
        except AsfynAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AsfynError as err:
            raise UpdateFailed(str(err)) from err
        self.config_entry.async_create_background_task(
            self.hass, self._async_import_statistics(meters), f"{DOMAIN}_statistics"
        )
        return meters

    async def _fetch(self) -> dict[str, Meter]:
        today = dt_util.now().date()
        meters: dict[str, Meter] = {}
        for customer in self.config_entry.data[CONF_CUSTOMERS]:
            data = await self.client.get_estates_with_products(customer)
            _LOGGER.debug(
                "Customer %s products: %s",
                customer,
                [
                    (p.get("category"), p.get("installationId"), p.get("subscriptionState"))
                    for e in data.get("estates") or []
                    for p in e.get("products") or []
                ],
            )
            for estate in data.get("estates") or []:
                for product in estate.get("products") or []:
                    category = product.get("category")
                    if category not in CATEGORY_UNITS:
                        continue
                    if product.get("subscriptionState") not in (None, "Active"):
                        continue
                    meter = Meter(
                        customer=customer,
                        estate_id=estate["id"],
                        installation=str(product["installationId"]),
                        meter=str(product.get("meterNumber") or ""),
                        category=category,
                        name=product.get("productName") or category,
                        meter_number=str(product.get("customerMeterNumber") or ""),
                        meter_type=product.get("meterType") or "",
                        address=estate.get("address") or "",
                    )
                    await self._fetch_meter(meter, today)
                    meters[meter.key] = meter
        return meters

    async def _fetch_meter(self, meter: Meter, today: date) -> None:
        info = await self.client.get_product(
            meter.customer, meter.installation, meter.category, meter.meter
        )
        meter.filters = info.get("availableFilters") or []
        meter.years = sorted(int(y) for y in info.get("availableYears") or [])
        for unit, key in (("m3", "latestReadingsM3"), ("GJ", "latestReadingsGJ")):
            reading = info.get(key)
            if reading and reading.get("value") is not None:
                meter.readings[unit] = (
                    float(reading["value"]),
                    _parse_date(reading.get("latestReading")),
                )

        for unit in meter.units:
            month = await self._month(meter, unit, today)
            meter.month[unit] = month.get("summary") or {}
            days = [d for d in daily_values(month, today) if not d[2]]
            if not days:
                previous = await self._month(meter, unit, today.replace(day=1) - timedelta(days=1))
                days = [d for d in daily_values(previous, today) if not d[2]]
            if days:
                meter.last_day[unit] = (days[-1][0], days[-1][1])
            if unit in STATISTIC_UNITS:
                year = await self.client.get_consumption(
                    meter.customer, meter.estate_id, meter.meter, meter.installation,
                    unit, "year", today,
                )
                meter.year[unit] = year.get("summary") or {}

    async def _month(self, meter: Meter, unit: str, day: date) -> dict:
        key = (unit, day.year, day.month)
        if key not in meter.month_cache:
            meter.month_cache[key] = await self.client.get_consumption(
                meter.customer, meter.estate_id, meter.meter, meter.installation,
                unit, "month", day,
            )
        return meter.month_cache[key]

    async def _async_import_statistics(self, meters: dict[str, Meter]) -> None:
        async with self._stats_lock:
            for meter in meters.values():
                for unit in meter.units:
                    if unit not in STATISTIC_UNITS:
                        continue
                    try:
                        await self._import_meter(meter, unit)
                    except AsfynError as err:
                        _LOGGER.warning("Statistics import for %s %s failed: %s", meter.key, unit, err)

    @staticmethod
    def statistic_id(meter: Meter, unit: str) -> str:
        return f"{DOMAIN}:{meter.category}_{meter.installation}_{meter.meter}_{unit}".lower()

    async def _import_meter(self, meter: Meter, unit: str) -> None:
        stat_id = self.statistic_id(meter, unit)
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        today = dt_util.now().date()
        tz = dt_util.get_default_time_zone()
        if last.get(stat_id):
            row = last[stat_id][0]
            last_day = dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date()
            total = float(row.get("sum") or 0)
            month = last_day.replace(day=1)
        else:
            last_day = None
            total = 0.0
            month = date(meter.years[0] if meter.years else today.year, 1, 1)

        stats: list[StatisticData] = []
        done = False
        while month <= today and not done:
            response = await self._month(meter, unit, month)
            for day, value, estimated in daily_values(response, today):
                if last_day and day <= last_day:
                    continue
                if estimated:
                    # Wait for the real reading before going past an estimate.
                    done = True
                    break
                total += value
                start = dt_util.as_utc(datetime(day.year, day.month, day.day, tzinfo=tz))
                stats.append(StatisticData(start=start, state=total, sum=total))
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            if not last_day:
                await asyncio.sleep(0.3)  # be gentle during the first backfill

        if not stats:
            return
        metadata = StatisticMetaData(
            source=DOMAIN,
            statistic_id=stat_id,
            name=f"{meter.name} {meter.address} ({UNIT_LABELS[unit]})",
            unit_of_measurement="m³" if unit == "m3" else unit,
            unit_class=UNIT_CLASS[unit],
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
        )
        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.debug("Imported %d days into %s", len(stats), stat_id)
