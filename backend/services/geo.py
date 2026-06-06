"""Geospatial helper utilities."""

from __future__ import annotations

import math


def distance_meters(lat1, lng1, lat2, lng2) -> float:
    """Return Haversine distance between two WGS84 coordinates in meters."""
    radius = 6_371_000
    lat1_f = float(lat1)
    lng1_f = float(lng1)
    lat2_f = float(lat2)
    lng2_f = float(lng2)

    phi1 = math.radians(lat1_f)
    phi2 = math.radians(lat2_f)
    dphi = math.radians(lat2_f - lat1_f)
    dlambda = math.radians(lng2_f - lng1_f)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))
