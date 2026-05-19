"""
Lead scraper — uses Google Places API as primary source.
Falls back to Yellow Pages HTML scraping only if no API key is configured.
"""
import re
import asyncio
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from places import search_businesses, get_place_details, PLACES_API_KEY

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def presence_score(website: str | None) -> int:
    if not website:
        return 0
    return 1


def presence_label(score: int) -> str:
    return {0: "No website", 1: "Weak website", 2: "Some presence"}.get(score, "Unknown")


async def scrape_leads(category: str, location: str, max_results: int = 25) -> list[dict]:
    """Fetch leads via Google Places API (reliable) or Yellow Pages fallback."""
    if PLACES_API_KEY:
        return await _scrape_google_places(category, location, max_results)
    return await _scrape_yellowpages(category, location, max_results)


async def _scrape_google_places(category: str, location: str, max_results: int) -> list[dict]:
    """Use Google Places to find businesses, then batch-fetch phone + website."""
    places = await search_businesses(category, location, max_results)

    async def enrich(place: dict) -> dict:
        details = await get_place_details(place["place_id"])
        website = details.get("website")
        phone = details.get("phone")
        score = presence_score(website)
        return {
            "name": place["name"],
            "phone": phone,
            "address": place["address"],
            "website": website,
            "business_type": place.get("business_type") or category,
            "presence_score": score,
            "presence_label": presence_label(score),
            "source": "google_places",
        }

    # Fetch details in parallel (cap at 10 concurrent to avoid rate limits)
    semaphore = asyncio.Semaphore(10)

    async def enrich_safe(place):
        async with semaphore:
            return await enrich(place)

    results = await asyncio.gather(*[enrich_safe(p) for p in places], return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def _scrape_yellowpages(category: str, location: str, max_results: int) -> list[dict]:
    """Fallback: scrape Yellow Pages directly (less reliable)."""
    url = (
        f"https://www.yellowpages.com/search"
        f"?search_terms={quote_plus(category)}"
        f"&geo_location_terms={quote_plus(location)}"
    )

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select(".result")[:max_results]:
        name_el = card.select_one(".business-name") or card.select_one("h2 a")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)

        phone_el = card.select_one(".phones .phone.primary") or card.select_one(".phone")
        phone = phone_el.get_text(strip=True) if phone_el else None

        street = card.select_one(".street-address")
        locality = card.select_one(".locality")
        parts = [x.get_text(strip=True) for x in [street, locality] if x]
        address = ", ".join(p for p in parts if p) or None

        website = None
        for a in card.select("a"):
            href = a.get("href", "")
            if href.startswith("http") and "yellowpages.com" not in href:
                website = href
                break

        cats = [c.get_text(strip=True) for c in card.select(".categories a")]
        business_type = ", ".join(cats) if cats else category
        score = presence_score(website)

        results.append({
            "name": name,
            "phone": phone,
            "address": address,
            "website": website,
            "business_type": business_type,
            "presence_score": score,
            "presence_label": presence_label(score),
            "source": "yellowpages",
        })

    return results


async def check_website_quality(url: str) -> dict:
    """Quick check: is the website alive and how basic is it?"""
    if not url:
        return {"alive": False, "score": 0}
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=8) as client:
            resp = await client.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        text = resp.text.lower()

        word_count = len(soup.get_text().split())
        img_count = len(soup.find_all("img"))

        signals = {
            "alive": resp.status_code < 400,
            "has_mobile": "viewport" in text,         # modern responsive site
            "has_content": word_count > 250,           # actual page content
            "has_images": img_count > 2,               # real designed pages have images
            "word_count": word_count,
            "page_size_kb": round(len(resp.content) / 1024, 1),
        }

        score = 0
        if signals["has_mobile"]:
            score += 1
        if signals["has_content"]:
            score += 1
        if signals["has_images"]:
            score += 1

        signals["quality_score"] = score
        return signals
    except Exception:
        return {"alive": False, "score": 0}
