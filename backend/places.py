import os
import asyncio
import httpx
from typing import Optional

PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
PLACES_BASE = "https://maps.googleapis.com/maps/api/place"


async def _fetch_page(client: httpx.AsyncClient, params: dict) -> tuple[list, Optional[str]]:
    resp = await client.get(f"{PLACES_BASE}/textsearch/json", params=params)
    data = resp.json()
    return data.get("results", []), data.get("next_page_token")


async def _search_one_query(query: str, location: str, key: str) -> list[dict]:
    """Fetch up to 3 pages (60 results) for a single query."""
    results = []
    next_page_token = None

    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(3):
            if next_page_token:
                await asyncio.sleep(3)  # Google requires delay before using token
                params = {"pagetoken": next_page_token, "key": key}
            else:
                params = {"query": f"{query} in {location}", "key": key}

            places, next_page_token = await _fetch_page(client, params)
            for p in places:
                results.append({
                    "place_id": p.get("place_id"),
                    "name": p.get("name"),
                    "address": p.get("formatted_address"),
                    "rating": p.get("rating"),
                    "review_count": p.get("user_ratings_total", 0),
                    "business_type": query,
                })
            if not next_page_token:
                break

    return results


async def search_businesses(query: str, location: str, max_results: int = 20) -> list[dict]:
    """Search Google Places using parallel query variations to exceed 20-result page limit."""
    key = os.getenv("GOOGLE_PLACES_API_KEY", "")
    if not key:
        return []

    # Build query variations to pull more diverse results
    variations = [query]
    if max_results > 20:
        variations += [
            f"{query} near me",
            f"local {query}",
        ]

    # Run all variations in parallel
    all_results = await asyncio.gather(
        *[_search_one_query(v, location, key) for v in variations],
        return_exceptions=True,
    )

    # Merge, deduplicate by place_id
    seen = set()
    merged = []
    for batch in all_results:
        if isinstance(batch, Exception):
            continue
        for r in batch:
            pid = r.get("place_id")
            if pid and pid not in seen:
                seen.add(pid)
                merged.append(r)

    return merged[:max_results]


async def get_place_details(place_id: str) -> dict:
    """Fetch phone, website, and review count for a specific place."""
    async with httpx.AsyncClient(timeout=15) as client:
        params = {
            "place_id": place_id,
            "fields": "name,formatted_phone_number,website,formatted_address,user_ratings_total,rating",
            "key": os.getenv("GOOGLE_PLACES_API_KEY", ""),
        }
        resp = await client.get(f"{PLACES_BASE}/details/json", params=params)
        result = resp.json().get("result", {})
        return {
            "phone": result.get("formatted_phone_number"),
            "website": result.get("website"),
            "review_count": result.get("user_ratings_total", 0),
            "rating": result.get("rating"),
        }
