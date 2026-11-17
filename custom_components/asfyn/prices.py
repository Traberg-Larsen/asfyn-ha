"""Fjernvarme Fyn tariffs, read from the price folders (PDF) on fjernvarmefyn.dk.

The current folder is linked from the consumer price page, and earlier ones from the
price history page. Each folder says "Gældende fra <day>. <month> <year>" and lists, in
the table "Fjernvarmepriser", the yearly meter charge and the price per GJ, per MWh and
per m3 of district heating water, each excluding and including VAT.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import io
import logging
import re
from urllib.parse import urljoin

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE = "https://www.fjernvarmefyn.dk"
CURRENT_PAGE = f"{BASE}/priser/overblik/forbrugspriser/"
HISTORY_PAGE = f"{BASE}/priser/overblik/forbrugspriser/prishistorik/"
TIMEOUT = aiohttp.ClientTimeout(total=120)
VAT = 1.25

MONTHS = {
    "januar": 1, "februar": 2, "marts": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}
NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")


@dataclass
class Tariff:
    """One price folder."""

    valid_from: str  # ISO date
    url: str
    meter_charge: float  # kr per year, incl. VAT
    per_gj: float  # kr per GJ, incl. VAT
    per_mwh: float  # kr per MWh, incl. VAT
    per_m3: float  # kr per m3 district heating water, incl. VAT
    effect_per_m2: float | None  # kr per m2 per year (first area tier), incl. VAT

    @property
    def start(self) -> date:
        return date.fromisoformat(self.valid_from)

    def as_dict(self) -> dict:
        return asdict(self)


def _num(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def _pairs(numbers: list[float], count: int) -> list[tuple[float, float]] | None:
    """Split 2*count numbers into (excl. VAT, incl. VAT) pairs, whatever the text order."""
    if len(numbers) < 2 * count:
        return None
    nums = numbers[: 2 * count]
    layouts = (
        list(zip(nums[:count], nums[count:])),  # all excl., then all incl.
        list(zip(nums[0::2], nums[1::2])),  # excl./incl. per row
    )
    for pairs in layouts:
        if all(abs(inc - ex * VAT) < 0.02 for ex, inc in pairs):
            return pairs
    return None


def parse_text(text: str, url: str) -> Tariff | None:
    """Pull the tariff out of the extracted text of one price folder."""
    flat = " ".join(text.split())
    valid = re.search(r"Gældende fra\s+(\d{1,2})\.\s*([a-zæøå]+)\s+(\d{4})", flat, re.I)
    if not valid or valid.group(2).lower() not in MONTHS:
        return None
    valid_from = date(int(valid.group(3)), MONTHS[valid.group(2).lower()], int(valid.group(1)))

    start = flat.find("Årligt målerbidrag")
    end = flat.find("Effektbidrag", start)
    if start < 0 or end < 0:
        return None
    main = _pairs([_num(n) for n in NUMBER.findall(flat[start:end])], 4)
    if not main:
        return None
    (_, meter), (_, gj), (_, mwh), (_, m3) = main

    effect = None
    eff_end = flat.find("Erhverv", end)
    eff = _pairs([_num(n) for n in NUMBER.findall(flat[end : eff_end if eff_end > 0 else None])], 4)
    if eff:
        effect = eff[0][1]

    # Sanity: MWh is 3.6 GJ.
    if abs(mwh - gj * 3.6) > 1:
        return None
    return Tariff(valid_from.isoformat(), url, meter, gj, mwh, m3, effect)


def extract_pdf_text(data: bytes) -> str:
    """Text of the first pages of a PDF (runs in an executor)."""
    from pypdf import PdfReader  # noqa: PLC0415 - heavy import, only when needed

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:3])


async def find_pdf_links(session: aiohttp.ClientSession) -> list[str]:
    """Price folder URLs: the current one first, then the history."""
    links: list[str] = []
    for page in (CURRENT_PAGE, HISTORY_PAGE):
        async with session.get(page, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            html = await resp.text()
        for href in re.findall(r'href="([^"]+\.pdf)"', html, re.I):
            name = href.rsplit("/", 1)[-1].lower()
            if "pris" in name and "erhverv" not in name:
                url = urljoin(BASE, href)
                if url not in links:
                    links.append(url)
    return links


async def download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        return await resp.read()


def tariff_for(tariffs: list[Tariff], day: date) -> Tariff | None:
    """The tariff in force on a day."""
    best = None
    for tariff in tariffs:
        if tariff.start <= day and (best is None or tariff.start > best.start):
            best = tariff
    return best
