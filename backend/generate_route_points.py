"""
generate_route_points.py
────────────────────────────────────────────────────────────────────────────────
One-time script — run this ONCE from your Python backend (or locally).
It reads your stops from Supabase, calls the public OSRM API to snap each
corridor's stops to real road geometry, then inserts the dense route points
back into the `route_points` table.

You do NOT need to run this again unless your stops change.

Requirements:
    pip install supabase requests python-dotenv

Usage:
    python generate_route_points.py

Environment variables needed (same .env.local / .env you already use):
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=eyJ...   ← use the service role key, not the anon key,
                                          because we're writing to the DB
────────────────────────────────────────────────────────────────────────────────
"""

import os
import time
import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(".env.local")   # Next.js convention; falls back to .env if absent
load_dotenv(".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
# Use service role key for writes — never expose this in frontend code
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# Public OSRM demo server — no key needed, but rate-limited.
# For production, self-host: https://github.com/Project-OSRM/osrm-backend
OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"

# OSRM allows up to 100 waypoints per request. Jakarta's corridors have
# 14–26 stops so we're well within the limit and don't need to chunk.
MAX_WAYPOINTS = 100

# Pause between corridor requests to be polite to the public OSRM server
REQUEST_DELAY_S = 1.5


def fetch_osrm_geometry(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """
    coords: list of (lat, lng) in stop order.
    Returns a dense list of (lat, lng) road-following waypoints decoded
    from the OSRM polyline response.
    """
    # OSRM expects lng,lat (GeoJSON order), not lat,lng
    waypoints = ";".join(f"{lng},{lat}" for lat, lng in coords)
    url = f"{OSRM_BASE}/{waypoints}"
    params = {
        "overview": "full",       # full geometry, not simplified
        "geometries": "geojson",  # easier to parse than encoded polyline
        "steps": "false",
    }

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {data.get('code')} — {data.get('message')}")

    # GeoJSON coordinates are [lng, lat]
    geojson_coords = data["routes"][0]["geometry"]["coordinates"]
    return [(lat, lng) for lng, lat in geojson_coords]


def clear_corridor_route_points(supabase: Client, corridor_id: str) -> None:
    """Delete existing route points for a corridor before re-inserting."""
    supabase.table("route_points").delete().eq("corridor_id", corridor_id).execute()


def insert_route_points(
    supabase: Client,
    corridor_id: str,
    points: list[tuple[float, float]],
) -> None:
    """Batch-insert route points. Supabase accepts up to 1000 rows per call."""
    rows = [
        {"corridor_id": corridor_id, "sequence": i, "latitude": lat, "longitude": lng}
        for i, (lat, lng) in enumerate(points)
    ]
    # Insert in chunks of 500 to stay well within Supabase limits
    chunk_size = 500
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        supabase.table("route_points").insert(chunk).execute()
    print(f"  ✓ Inserted {len(rows)} route points")


def main() -> None:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Fetch all corridors
    corridors_resp = (
        supabase.table("corridors")
        .select("id, corridor_code, name")
        .order("corridor_code")
        .execute()
    )
    corridors = corridors_resp.data
    print(f"Found {len(corridors)} corridors\n")

    for corridor in corridors:
        cid = corridor["id"]
        name = corridor["name"]
        print(f"Processing corridor {corridor['corridor_code']} — {name}")

        # Fetch stops for this corridor in sequence order
        stops_resp = (
            supabase.table("stops")
            .select("latitude, longitude, sequence")
            .eq("corridor_id", cid)
            .order("sequence")
            .execute()
        )
        stops = stops_resp.data

        if len(stops) < 2:
            print(f"  ⚠ Skipped — fewer than 2 stops")
            continue

        coords = [(s["latitude"], s["longitude"]) for s in stops]
        print(f"  {len(coords)} stops → calling OSRM…")

        try:
            # OSRM waypoint limit: chunk if needed (future-proofing)
            if len(coords) <= MAX_WAYPOINTS:
                route_points = fetch_osrm_geometry(coords)
            else:
                # Chunk with 1-point overlap so segments connect cleanly
                route_points = []
                for i in range(0, len(coords), MAX_WAYPOINTS - 1):
                    chunk = coords[i : i + MAX_WAYPOINTS]
                    segment = fetch_osrm_geometry(chunk)
                    if route_points:
                        segment = segment[1:]  # drop duplicate junction point
                    route_points.extend(segment)

            print(f"  OSRM returned {len(route_points)} road geometry points")

            clear_corridor_route_points(supabase, cid)
            insert_route_points(supabase, cid, route_points)

        except Exception as e:
            print(f"  ✗ Failed: {e}")

        # Be polite to the public OSRM server
        time.sleep(REQUEST_DELAY_S)

    print("\nDone. All route_points populated.")


if __name__ == "__main__":
    main()