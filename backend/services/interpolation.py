import math


def compute_bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlng = math.radians(lng2 - lng1)
    y = math.sin(dlng) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlng)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def get_bus_position(bus_id: str, stops: list, now: int) -> dict | None:
    if not stops or now < stops[0]["waktu_tiba_detik"] or now > stops[-1]["waktu_tiba_detik"]:
        return None
    for i in range(len(stops) - 1):
        a, b = stops[i], stops[i + 1]
        if a["waktu_tiba_detik"] <= now <= b["waktu_tiba_detik"]:
            denom = b["waktu_tiba_detik"] - a["waktu_tiba_detik"]
            pct = 0.0 if denom == 0 else (now - a["waktu_tiba_detik"]) / denom
            lat = a["lat"] + (b["lat"] - a["lat"]) * pct
            lng = a["lng"] + (b["lng"] - a["lng"]) * pct
            return {
                "bus_id": bus_id,
                "koridor_id": a["koridor_id"],
                "lat": lat,
                "lng": lng,
                "bearing": compute_bearing(a["lat"], a["lng"], b["lat"], b["lng"]),
                "next_stop": b["nama_halte"],
                "eta_minutes": round((b["waktu_tiba_detik"] - now) / 60),
            }
    return None
