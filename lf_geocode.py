#!/usr/bin/env python3
"""
lf_geocode.py - Lead Finder Geocoding Module
=============================================
Cache-first city geocoding with haversine distance math.
Seed data: CA_CITY_COORDS (~441 cities) + geocode_cache.json (292 entries).
Falls back to Google Geocoding API for new cities.
"""

import math, json, time
from pathlib import Path
from typing import Optional

from lf_config import get, google_maps_api_key, rate_limit

BASE_DIR = Path(__file__).parent

# ── Haversine distance (miles) ────────────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8  # Earth radius in miles
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(Δλ/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def proximity_level(dist_mi: float) -> int:
    """Map distance in miles to proximity tier 1-5."""
    if dist_mi <= 5:   return 1
    if dist_mi <= 10:  return 2
    if dist_mi <= 15:  return 3
    if dist_mi <= 20:  return 4
    if dist_mi <= 25:  return 5
    return 0  # outlier


# ── In-memory city coordinates from seed files ────────────────────────────────
def _load_seed_coords() -> dict:
    coords = {}
    # CA city coords (441 cities)
    ca_path = BASE_DIR / "ca_city_coords.json"
    if ca_path.exists():
        with open(ca_path) as f:
            data = json.load(f)
        for city, entry in data.items():
            coords[(city.lower(), "ca")] = (entry["lat"], entry["lng"])
    # Extended geocode cache (292 entries)
    cache_path = BASE_DIR / "geocode_cache.json"
    if cache_path.exists():
        with open(cache_path) as f:
            data = json.load(f)
        for city, entry in data.items():
            coords[(city.lower(), "ca")] = (entry["lat"], entry["lng"])
    return coords

_SEED_COORDS = _load_seed_coords()


def get_city_coords(city: str, state: str = "CA") -> Optional[dict]:
    """
    Returns {'lat': float, 'lng': float, 'source': str} for a city.
    Priority: in-memory seed cache -> database cache -> Google Geocoding API.
    """
    from lf_db import get_geocode, upsert_geocode

    key = (city.lower().strip(), state.upper().strip())

    # 1. In-memory seed
    if key in _SEED_COORDS:
        lat, lng = _SEED_COORDS[key]
        return {"lat": lat, "lng": lng, "source": "CITY_DICT_OK"}

    # 2. Database cache
    row = get_geocode(city, state)
    if row:
        return {"lat": row["lat"], "lng": row["lng"], "source": row["source"]}

    # 3. Google Geocoding API
    api_key = google_maps_api_key()
    if not api_key:
        return None

    import requests
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": f"{city}, {state}", "key": api_key}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("status") == "OK" and data["results"]:
            result = data["results"][0]
            loc = result["geometry"]["location"]
            lat, lng = loc["lat"], loc["lng"]
            source = "GOOGLE_OK"
            # Cache it
            upsert_geocode(city, state, lat, lng, source)
            time.sleep(0.5)  # rate limit
            return {"lat": lat, "lng": lng, "source": source}
    except Exception as e:
        print(f"[geocode] Google Geocoding API error: {e}")

    return None


def get_location_bias(city: str, state: str = "CA", radius_miles: int = 25) -> dict:
    """
    Returns {'lat', 'lng', 'radius_meters', 'source'} for location-biased search.
    """
    coords = get_city_coords(city, state)
    if not coords:
        return None
    return {
        "lat": coords["lat"],
        "lng": coords["lng"],
        "radius_meters": int(radius_miles * 1609.34),
        "source": coords["source"]
    }


def infer_is_ca(city: str) -> bool:
    """Returns True if city is known to be in California."""
    return city.lower().strip() in {c for _, c in _SEED_COORDS}


def find_nearby_cities(center_city: str, state: str = "CA", radius_miles: int = 25) -> list:
    """
    Find all seed cities within radius_miles of center_city.
    Returns list of (city, state, lat, lng, distance_mi).
    """
    center = get_city_coords(center_city, state)
    if not center:
        return []

    results = []
    for (city_lower, st), (lat, lng) in _SEED_COORDS.items():
        dist = haversine(center["lat"], center["lng"], lat, lng)
        if dist <= radius_miles:
            results.append((city_lower, st.upper(), lat, lng, dist))
    results.sort(key=lambda x: x[4])
    return results
