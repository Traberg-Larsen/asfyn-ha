"""Client for the Administrationsservice Fyn (asfyn.dk) self-service API.

Logs in through the forsyningslogin.dk login form like the asfyn.dk website does
(OAuth authorization code with PKCE), then keeps the session alive with the refresh
token. The site's public OAuth client settings are read from asfyn.dk at runtime.
"""

from __future__ import annotations

import base64
from datetime import date
import hashlib
import html
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

SITE = "https://www.asfyn.dk"
SSO = "https://accounts.forsyningslogin.dk"
REDIRECT_URI = f"{SITE}/select-customer/"
SCOPE = "openid profile customer offline_access"
TIMEOUT = aiohttp.ClientTimeout(total=30)
# The API only returns the asfyn products (heat, water, sewer) with a Danish culture.
API_HEADERS = {"Accept": "application/json", "Accept-Language": "da-DK"}


class AsfynError(Exception):
    """Error talking to asfyn."""


class AsfynAuthError(AsfynError):
    """Login was refused."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _nuxt_value(page: str, key: str) -> str | None:
    match = re.search(key + r':"((?:[^"\\]|\\.)*)"', page)
    if not match:
        return None
    return json.loads(f'"{match.group(1)}"')


class AsfynClient:
    """Minimal async client for the asfyn self-service API."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._settings: dict[str, str] | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at = 0.0

    async def _load_settings(self) -> dict[str, str]:
        if self._settings:
            return self._settings
        async with self._session.get(f"{SITE}/forside/", timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            page = await resp.text()
        settings = {
            "client_id": _nuxt_value(page, "SSO_CLIENT_ID"),
            "client_secret": _nuxt_value(page, "SSO_CLIENT_SECRET"),
            "selfservice": _nuxt_value(page, "SELFSERVICE_API_URL"),
            "profile": _nuxt_value(page, "PROFILE_SERVICE"),
        }
        missing = [k for k, v in settings.items() if not v]
        if missing:
            raise AsfynError(f"asfyn.dk page no longer has {', '.join(missing)}")
        self._settings = {k: v.rstrip("/") for k, v in settings.items() if v}
        return self._settings

    async def login(self) -> None:
        """Log in with e-mail and password through the login form."""
        settings = await self._load_settings()
        self._session.cookie_jar.clear()
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        params = {
            "client_id": settings["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "state": secrets.token_hex(16),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        try:
            async with self._session.get(
                f"{SSO}/connect/authorize", params=params, timeout=TIMEOUT
            ) as resp:
                login_url = str(resp.url)
                page = await resp.text()
            token = re.search(
                r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page
            )
            return_url = re.search(r'name="ReturnUrl"[^>]*value="([^"]*)"', page)
            if not token or not return_url:
                raise AsfynError("Login form not found on forsyningslogin.dk")
            form = {
                "ReturnUrl": html.unescape(return_url.group(1)),
                "Username": self._email,
                "Password": self._password,
                "button": "login",
                "__RequestVerificationToken": token.group(1),
            }
            async with self._session.post(
                login_url, data=form, allow_redirects=False, timeout=TIMEOUT
            ) as resp:
                if resp.status != 302:
                    raise AsfynAuthError("E-mail or password was refused")
                location = urljoin(login_url, resp.headers["Location"])

            code = None
            for _ in range(8):
                if location.startswith(REDIRECT_URI):
                    code = parse_qs(urlparse(location).query).get("code", [None])[0]
                    break
                async with self._session.get(
                    location, allow_redirects=False, timeout=TIMEOUT
                ) as resp:
                    if "Location" not in resp.headers:
                        break
                    location = urljoin(location, resp.headers["Location"])
            if not code:
                raise AsfynError("Login did not return an authorization code")

            await self._token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                }
            )
        except aiohttp.ClientError as err:
            raise AsfynError(f"Login failed: {err}") from err

    async def _token(self, data: dict[str, str]) -> None:
        settings = await self._load_settings()
        data = {
            **data,
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
        }
        async with self._session.post(
            f"{SSO}/connect/token", data=data, timeout=TIMEOUT
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                if body.get("error") == "invalid_grant":
                    raise AsfynAuthError(body.get("error"))
                raise AsfynError(f"Token request failed: {resp.status} {body}")
        self._access_token = body["access_token"]
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        self._expires_at = time.time() + int(body.get("expires_in", 3600))

    async def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 120:
            return self._access_token
        if self._refresh_token:
            try:
                await self._token(
                    {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
                )
                return self._access_token  # type: ignore[return-value]
            except AsfynError:
                _LOGGER.debug("Refresh token refused; logging in again")
                self._refresh_token = None
        await self.login()
        return self._access_token  # type: ignore[return-value]

    async def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        settings = await self._load_settings()
        url = f"{settings[base]}{path}"
        for attempt in (1, 2):
            token = await self._ensure_token()
            headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
            try:
                async with self._session.get(
                    url, params=params, headers=headers, timeout=TIMEOUT
                ) as resp:
                    if resp.status == 401 and attempt == 1:
                        self._access_token = None
                        continue
                    if resp.status == 404:
                        return None
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except aiohttp.ClientError as err:
                raise AsfynError(f"GET {path} failed: {err}") from err
        raise AsfynAuthError("API refused the token")

    async def get_customers(self) -> list[str]:
        """Customer numbers linked to the login."""
        data = await self._get("profile", "/api/customers/") or []
        return [str(c["customerNumber"]) for c in data if c.get("customerNumber")]

    async def get_estates_with_products(self, customer: str) -> dict:
        return await self._get("selfservice", f"/api/customers/{customer}/estateswithproducts") or {}

    async def get_product(self, customer: str, installation: str, category: str, meter: str) -> dict:
        return (
            await self._get(
                "selfservice",
                f"/api/customers/{customer}/installations/{installation}/products",
                {"category": category, "meterId": meter},
            )
            or {}
        )

    async def get_consumption(
        self,
        customer: str,
        estate: int | str,
        meter: str,
        installation: str,
        unit: str,
        period: str,
        day: date,
    ) -> dict:
        """Consumption for the period (week, month, quarter, year) that holds `day`."""
        params = {
            "year": day.year,
            "quarter": (day.month - 1) // 3 + 1,
            "week": day.isocalendar()[1],
            "month": day.month,
            "day": day.day,
        }
        return (
            await self._get(
                "selfservice",
                f"/api/consumptions/{customer}/{estate}/{meter}/{installation}/{unit}/{period}",
                params,
            )
            or {}
        )
