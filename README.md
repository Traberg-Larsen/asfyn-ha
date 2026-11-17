# Administrationsservice Fyn (asfyn.dk) for Home Assistant

Unofficial custom integration that reads district heating (Fjernvarme Fyn) and water/sewer meter data from the self-service site [asfyn.dk](https://www.asfyn.dk/selvbetjening/).

It is not made or supported by Administrationsservice Fyn. It uses the same API as the asfyn.dk website, so it can break if they change the site.

## What you get

For each heat meter (one device per meter):

| Sensor | Unit |
| --- | --- |
| Energy this year / this month / latest day | GJ |
| Volume this year / this month / latest day | m³ |
| Cooling latest day, average cooling this month | °C |
| Compared to similar homes (this month) | % |
| Meter reading energy / volume (last yearly reading) | GJ / m³ |

Water and sewer meters that only have a yearly reading get the meter reading sensor.

Daily consumption is also imported as long-term statistics (`asfyn:heat_<installation>_<meter>_gj` and `_m3`) on the day it belongs to. asfyn publishes data about a day late, so use these statistics in the **Energy dashboard** (for example as gas/heat consumption) rather than the sensors. On first setup the integration backfills every year asfyn has.

The integration polls every 4 hours.

## Install

1. In the Marketplace (HACS), add this repository as a custom repository of type *Integration*.
2. Download **Administrationsservice Fyn (asfyn.dk)** and restart Home Assistant.
3. Settings → Devices & services → Add integration → *Administrationsservice Fyn*.
4. Log in with the e-mail and password you use on asfyn.dk. MitID logins cannot be used; create an e-mail login on asfyn.dk first.

## How it works

The integration logs in through the forsyningslogin.dk login form, the same way the website does (OAuth authorization code with PKCE), and keeps the session alive with the refresh token. The site's public OAuth client settings are read from asfyn.dk each time it starts, so nothing secret is stored in this repository. Your e-mail and password are stored in Home Assistant's config entry.
