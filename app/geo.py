"""Shared geospatial helpers."""

# ~11 m at the equator. A receiver with no lock sometimes still emits a
# coordinate, and that coordinate is (0, 0) — "null island" in the Atlantic off
# West Africa. Treat anything that close to the origin as no fix, so the map is
# never dragged into the ocean by a panic or event sent without a real lock.
_ZERO_EPS = 1e-4


def has_fix(lat: float | None, lon: float | None) -> bool:
    """True only for a real GPS fix; None or (0, 0) is treated as no fix."""
    if lat is None or lon is None:
        return False
    return not (abs(lat) < _ZERO_EPS and abs(lon) < _ZERO_EPS)


def clean_fix(lat: float | None, lon: float | None) -> tuple[float | None, float | None]:
    """Return (lat, lon) when it is a real fix, else (None, None)."""
    return (lat, lon) if has_fix(lat, lon) else (None, None)
