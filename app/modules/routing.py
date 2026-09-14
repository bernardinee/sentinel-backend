"""Road routing for dispatch: which unit actually reaches the scene soonest.

Uses the public OSRM demo server — free and keyless, the same constraint that
drives the dashboard's OSM basemap (a routing key that expires mid-defence is
exactly the failure we are avoiding).

Straight-line distance is NOT good enough for dispatch: in Accra the nearest
unit as the crow flies is routinely not the fastest by road once the ring roads,
one-ways and the Korle lagoon are taken into account. So candidates are
coarse-filtered by haversine and then routed properly.

If OSRM is unreachable the caller still gets a usable answer, explicitly marked
`source: "straight_line"` so the UI can say the ETA is an estimate rather than
quietly presenting a guess as a road route.
"""
import asyncio
import logging
import math
import time

import httpx

log = logging.getLogger(__name__)

OSRM_BASE = "https://router.project-osrm.org"
OSRM_TIMEOUT_S = 8.0
# Fallback speed when OSRM is unavailable. Deliberately conservative for urban
# Accra with a blue-light allowance.
FALLBACK_SPEED_KMH = 32.0
# Road distance exceeds straight-line distance; this is the usual urban factor.
FALLBACK_DETOUR_FACTOR = 1.35

_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL_S = 300


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _straight_line_route(from_lat, from_lon, to_lat, to_lon) -> dict:
    km = haversine_km(from_lat, from_lon, to_lat, to_lon) * FALLBACK_DETOUR_FACTOR
    return {
        "distance_km": round(km, 2),
        "duration_min": round(km / FALLBACK_SPEED_KMH * 60, 1),
        "geometry": [[from_lon, from_lat], [to_lon, to_lat]],
        "source": "straight_line",
    }


async def road_route(from_lat: float, from_lon: float,
                     to_lat: float, to_lon: float,
                     with_geometry: bool = True) -> dict:
    """Fastest driving route. Never raises — falls back to a marked estimate."""
    key = (round(from_lat, 4), round(from_lon, 4),
           round(to_lat, 4), round(to_lon, 4), with_geometry)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]

    url = (f"{OSRM_BASE}/route/v1/driving/"
           f"{from_lon},{from_lat};{to_lon},{to_lat}")
    params = {
        "overview": "full" if with_geometry else "false",
        "geometries": "geojson",
        "alternatives": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=OSRM_TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
        data = resp.json()
        if resp.status_code == 200 and data.get("code") == "Ok" and data.get("routes"):
            r = data["routes"][0]
            result = {
                "distance_km": round(r["distance"] / 1000.0, 2),
                "duration_min": round(r["duration"] / 60.0, 1),
                "geometry": (r.get("geometry", {}).get("coordinates", [])
                             if with_geometry else []),
                "source": "osrm",
            }
            _CACHE[key] = (time.time(), result)
            return result
        log.warning("OSRM returned %s / %s", resp.status_code, data.get("code"))
    except Exception as exc:
        log.warning("OSRM unreachable (%s) — falling back to straight line", exc)

    result = _straight_line_route(from_lat, from_lon, to_lat, to_lon)
    _CACHE[key] = (time.time(), result)
    return result


async def route_many(origins: list[tuple[str, float, float]],
                     to_lat: float, to_lon: float,
                     with_geometry: bool = False) -> dict[str, dict]:
    """Route several origins to one destination concurrently.

    `origins` is [(key, lat, lon), …]. Keep the list short — these hit a public
    demo server, so callers coarse-filter by haversine first.
    """
    results = await asyncio.gather(*[
        road_route(lat, lon, to_lat, to_lon, with_geometry)
        for _, lat, lon in origins
    ], return_exceptions=True)
    out: dict[str, dict] = {}
    for (key, lat, lon), res in zip(origins, results):
        out[key] = (res if isinstance(res, dict)
                    else _straight_line_route(lat, lon, to_lat, to_lon))
    return out
