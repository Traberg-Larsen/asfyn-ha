"""Data coordinator for asfyn: polls the API and imports daily statistics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging

import aiohttp

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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import prices
from .api import AsfynAuthError, AsfynClient, AsfynError
from .const import CATEGORY_UNITS, CONF_CUSTOMERS, DOMAIN, STATISTIC_UNITS, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

UNIT_LABELS = {"GJ": "GJ", "m3": "m³", "C": "°C"}
UNIT_CLASS = {"GJ": "energy", "m3": "volume", "C": "temperature"}
PRICE_CHECK_INTERVAL = timedelta(hours=24)


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
    invoices: list[dict] = field(default_factory=list)
    month_cache: dict[tuple[str, int, int], dict] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.customer}_{self.installation}_{self.meter}"

    @property
    def units(self) -> list[str]:
        if "month" not in self.filters:
            return []
        return CATEGORY_UNITS.get(self.category, [])


@dataclass
class AsfynData:
    """Everything the sensors read."""

    meters: dict[str, Meter]
    tariffs: list[prices.Tariff]

    def tariff_today(self) -> prices.Tariff | None:
        return prices.tariff_for(self.tariffs, dt_util.now().date())


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


def statistic_id(meter: Meter, kind: str) -> str:
    return f"{DOMAIN}:{meter.category}_{meter.installation}_{meter.meter}_{kind}".lower()


class AsfynCoordinator(DataUpdateCoordinator[AsfynData]):
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
        self._price_store: Store[dict] = Store(hass, 1, f"{DOMAIN}.prices")
        self._price_cache: dict | None = None
        self._tariffs: list[prices.Tariff] = []

    async def _async_update_data(self) -> AsfynData:
        try:
            meters = await self._fetch()
        except AsfynAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AsfynError as err:
            raise UpdateFailed(str(err)) from err
        if any(m.category == "heat" for m in meters.values()):
            await self._refresh_tariffs(meters)
        data = AsfynData(meters, list(self._tariffs))
        self.config_entry.async_create_background_task(
            self.hass, self._async_import_statistics(data), f"{DOMAIN}_statistics"
        )
        return data

    # ---------------------------------------------------------------- asfyn

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
        try:
            meter.invoices = await self.client.get_invoices(meter.customer, meter.installation)
        except AsfynError as err:
            _LOGGER.debug("Invoices for %s failed: %s", meter.key, err)

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

    # --------------------------------------------------------------- prices

    async def _refresh_tariffs(self, meters: dict[str, Meter]) -> None:
        """Read new Fjernvarme Fyn price folders, at most once a day."""
        if self._price_cache is None:
            self._price_cache = await self._price_store.async_load() or {"parsed": {}, "checked": None}
            self._tariffs = [
                prices.Tariff(**t) for t in self._price_cache["parsed"].values() if t
            ]
        checked = self._price_cache.get("checked")
        if checked and dt_util.utcnow() - dt_util.parse_datetime(checked) < PRICE_CHECK_INTERVAL:
            return

        first_year = min((y for m in meters.values() for y in m.years), default=dt_util.now().year)
        need_from = date(first_year, 1, 1)
        session = async_get_clientsession(self.hass)
        try:
            links = await prices.find_pdf_links(session)
            parsed: dict = self._price_cache["parsed"]
            for url in links:
                if url not in parsed:
                    try:
                        data = await prices.download(session, url)
                        text = await self.hass.async_add_executor_job(prices.extract_pdf_text, data)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                        _LOGGER.warning("Could not download %s: %s", url, err)
                        continue
                    except Exception:  # noqa: BLE001 - a broken PDF must not stop the rest
                        _LOGGER.warning("Could not read %s", url, exc_info=True)
                        parsed[url] = None
                        continue
                    tariff = prices.parse_text(text, url)
                    if tariff is None:
                        _LOGGER.warning("No tariff found in %s", url)
                    parsed[url] = tariff.as_dict() if tariff else None
                tariff_dict = parsed.get(url)
                if tariff_dict and date.fromisoformat(tariff_dict["valid_from"]) <= need_from:
                    break  # older folders are not needed for our data
            self._price_cache["checked"] = dt_util.utcnow().isoformat()
            await self._price_store.async_save(self._price_cache)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Could not check Fjernvarme Fyn prices: %s", err)
        self._tariffs = [prices.Tariff(**t) for t in self._price_cache["parsed"].values() if t]

    # ----------------------------------------------------------- statistics

    async def _async_import_statistics(self, data: AsfynData) -> None:
        async with self._stats_lock:
            for meter in data.meters.values():
                for unit in meter.units:
                    try:
                        await self._import_meter(meter, unit)
                    except AsfynError as err:
                        _LOGGER.warning("Statistics import for %s %s failed: %s", meter.key, unit, err)
                if meter.category == "heat" and {"GJ", "m3"} <= set(meter.units) and data.tariffs:
                    try:
                        await self._import_cost(meter, data.tariffs)
                    except AsfynError as err:
                        _LOGGER.warning("Cost import for %s failed: %s", meter.key, err)

    async def _last(self, stat_id: str, kind: str) -> tuple[date | None, float]:
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {kind}
        )
        if not last.get(stat_id):
            return None, 0.0
        row = last[stat_id][0]
        day = dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date()
        return day, float(row.get(kind) or 0)

    async def _days(self, meter: Meter, unit: str, start: date, today: date):
        """Yield (day, value) month by month from `start`, stopping at an estimate."""
        month = start.replace(day=1)
        while month <= today:
            response = await self._month(meter, unit, month)
            for day, value, estimated in daily_values(response, today):
                if day < start:
                    continue
                if estimated:
                    return
                yield day, value
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            await asyncio.sleep(0.2)

    @staticmethod
    def _start(day: date) -> datetime:
        tz = dt_util.get_default_time_zone()
        return dt_util.as_utc(datetime(day.year, day.month, day.day, tzinfo=tz))

    async def _import_meter(self, meter: Meter, unit: str) -> None:
        today = dt_util.now().date()
        cumulative = unit in STATISTIC_UNITS
        stat_id = statistic_id(meter, unit)
        last_day, total = await self._last(stat_id, "sum" if cumulative else "mean")
        start = last_day + timedelta(days=1) if last_day else date(
            meter.years[0] if meter.years else today.year, 1, 1
        )
        stats: list[StatisticData] = []
        async for day, value in self._days(meter, unit, start, today):
            if cumulative:
                total += value
                stats.append(StatisticData(start=self._start(day), state=total, sum=total))
            else:
                stats.append(
                    StatisticData(start=self._start(day), mean=value, min=value, max=value)
                )
        if not stats:
            return
        label = {"GJ": "", "m3": " vand", "C": " afkøling"}[unit]
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                source=DOMAIN,
                statistic_id=stat_id,
                name=f"{meter.name}{label} {meter.address} ({UNIT_LABELS[unit]})",
                unit_of_measurement="m³" if unit == "m3" else ("°C" if unit == "C" else unit),
                unit_class=UNIT_CLASS[unit],
                has_sum=cumulative,
                mean_type=StatisticMeanType.NONE if cumulative else StatisticMeanType.ARITHMETIC,
            ),
            stats,
        )
        _LOGGER.debug("Imported %d days into %s", len(stats), stat_id)

    async def _import_cost(self, meter: Meter, tariffs: list[prices.Tariff]) -> None:
        """Daily heat cost: GJ x price per GJ + m3 x price per m3, at each day's tariff."""
        today = dt_util.now().date()
        stat_id = statistic_id(meter, "cost")
        last_day, total = await self._last(stat_id, "sum")
        if last_day:
            start = last_day + timedelta(days=1)
        else:
            first_tariff = min(t.start for t in tariffs)
            first_data = date(meter.years[0] if meter.years else today.year, 1, 1)
            start = max(first_tariff, first_data)
        energy = {d: v async for d, v in self._days(meter, "GJ", start, today)}
        water = {d: v async for d, v in self._days(meter, "m3", start, today)}
        stats: list[StatisticData] = []
        for day in sorted(energy):
            tariff = prices.tariff_for(tariffs, day)
            if tariff is None or day not in water:
                break
            total += energy[day] * tariff.per_gj + water[day] * tariff.per_m3
            stats.append(StatisticData(start=self._start(day), state=round(total, 2), sum=round(total, 2)))
        if not stats:
            return
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                source=DOMAIN,
                statistic_id=stat_id,
                name=f"{meter.name} udgift {meter.address}",
                unit_of_measurement=self.hass.config.currency,
                unit_class=None,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
            ),
            stats,
        )
        _LOGGER.debug("Imported %d days into %s", len(stats), stat_id)
