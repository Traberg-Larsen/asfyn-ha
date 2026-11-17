"""Administrationsservice Fyn (asfyn.dk): district heating and water data."""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import AsfynClient
from .coordinator import AsfynCoordinator

PLATFORMS = [Platform.SENSOR]

type AsfynConfigEntry = ConfigEntry[AsfynCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AsfynConfigEntry) -> bool:
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = AsfynClient(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    coordinator = AsfynCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AsfynConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
