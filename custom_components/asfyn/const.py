"""Constants for the asfyn integration."""

from datetime import timedelta

DOMAIN = "asfyn"
MANUFACTURER = "Administrationsservice Fyn"
CONF_CUSTOMERS = "customers"
UPDATE_INTERVAL = timedelta(hours=4)

# Product categories we read, and the units each one has daily data in.
CATEGORY_UNITS: dict[str, list[str]] = {
    "heat": ["GJ", "m3", "C"],
    "water": ["m3"],
    "kloak": ["m3"],
}
# Units that are cumulative and go into long-term statistics (Energy dashboard).
STATISTIC_UNITS = {"GJ", "m3"}
