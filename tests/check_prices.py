"""Read Fjernvarme Fyn's live price folders with the integration's parser.

Run in CI: python tests/check_prices.py
"""

import asyncio
import importlib.util
from pathlib import Path
import sys

import aiohttp

spec = importlib.util.spec_from_file_location(
    "prices", Path(__file__).parents[1] / "custom_components" / "asfyn" / "prices.py"
)
prices = importlib.util.module_from_spec(spec)
sys.modules["prices"] = prices  # dataclasses look the module up here
spec.loader.exec_module(prices)


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        links = await prices.find_pdf_links(session)
        print(f"{len(links)} price folders found")
        found = []
        for url in links[:9]:
            data = await prices.download(session, url)
            tariff = prices.parse_text(prices.extract_pdf_text(data), url)
            print(url, "->", tariff)
            if tariff:
                found.append(tariff)
    if not found or found[0].url != links[0]:
        print("FAILED: the current price folder could not be read")
        return 1
    return 0


sys.exit(asyncio.run(main()))
