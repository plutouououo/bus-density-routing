"""GTFS-derived bus/trip simulation and ridership-based crowding.

The generated ``bus_id`` values in this module are simulated trip instances,
not real physical fleet vehicle IDs. Instances are generated from
``gtfs_frequencies`` and remain active from the first stop departure until the
last stop arrival.
"""

from __future__ import annotations

import hashlib
import os
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from services.geo import distance_meters
from services.interpolation import get_bus_position
from services.bus_selector import MAX_ETA_DETIK_DEFAULT

BUS_CAPACITY = 80
FALLBACK_LOAD_FACTOR = 0.5
LOAD_FACTOR_OUTPUT_CAP = float(os.getenv("LOAD_FACTOR_OUTPUT_CAP", "1.0") or 1.0)
SIMULATION_RUN_ID_DEFAULT = "default"
PROCESS_SIMULATION_RUN_ID = os.getenv("SIMULATION_RUN_ID") or f"run-{uuid.uuid4().hex[:8]}"
RIDERSHIP_RANDOM_SAMPLE_SIZE = 30
SCOPED_KORIDOR = {"1", "2", "3", "4", "5"}
DEFAULT_DEBUG_TIME = 5 * 3600
AUDIT_SAMPLE_TIMES = {
    "05_00": 5 * 3600,
    "06_00": 6 * 3600,
    "10_00": 10 * 3600,
    "12_00": 12 * 3600,
    "17_00": 17 * 3600,
    "22_00": 22 * 3600,
}
OVERLAP_GROUP_THRESHOLD = 0.80
MAX_VISIBLE_BUSES_PER_CORRIDOR_DIRECTION = int(
    os.getenv("MAX_VISIBLE_BUSES_PER_CORRIDOR_DIRECTION", "0") or 0
)
TIME_BANDS = (
    {
        "name": "early",
        "time_range": "05:00-06:00",
        "start": 5 * 3600,
        "end": 6 * 3600,
        "weight": 0.70,
    },
    {
        "name": "morning_peak",
        "time_range": "06:00-09:00",
        "start": 6 * 3600,
        "end": 9 * 3600,
        "weight": 1.40,
    },
    {
        "name": "midday_offpeak",
        "time_range": "09:00-16:00",
        "start": 9 * 3600,
        "end": 16 * 3600,
        "weight": 0.85,
    },
    {
        "name": "evening_peak",
        "time_range": "16:00-20:00",
        "start": 16 * 3600,
        "end": 20 * 3600,
        "weight": 1.35,
    },
    {
        "name": "night",
        "time_range": "20:00-22:00",
        "start": 20 * 3600,
        "end": 22 * 3600,
        "weight": 0.55,
    },
    {
        "name": "late_night",
        "time_range": "22:00-05:00",
        "start": 22 * 3600,
        "end": 5 * 3600,
        "weight": 0.20,
    },
)
TIME_BAND_BY_NAME = {band["name"]: band for band in TIME_BANDS}


@dataclass(frozen=True)
class SimulationContext:
    instances: list[dict]
    jadwal: dict[str, list[dict]]
    trip_supply_per_koridor: dict[str, int]
    daily_mean_load_factor: dict[tuple[str, str], float]
    latest_date: str | None
    latest_date_per_koridor: dict[str, str]
    recent_dates_per_koridor: dict[str, list[str]]
    ridership_by_date_koridor: dict[tuple[str, str], float]
    segmen_by_id: dict[str, dict]
    fallback_jadwal: dict[str, list[dict]]
    crowding_cache: dict[tuple[str | None, str], dict] = field(default_factory=dict)


def parse_gtfs_time(value: Any) -> int:
    """Parse GTFS HH:MM:SS into seconds, allowing hours >= 24."""
    if value is None:
        raise ValueError("GTFS time is None")
    parts = str(value).strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid GTFS time: {value!r}")
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


def _fetch_semua(supabase, nama_tabel: str, kolom: str = "*") -> list[dict]:
    hasil: list[dict] = []
    offset = 0
    while True:
        chunk = (
            supabase.table(nama_tabel)
            .select(kolom)
            .range(offset, offset + 999)
            .execute()
            .data
        )
        if not chunk:
            break
        hasil.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return hasil


def _stable_int_seed(*parts: Any) -> int:
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _clamped_normal(seed: int, mean: float, std: float, low: float, high: float) -> float:
    rng = random.Random(seed)
    return max(low, min(high, rng.normalvariate(mean, std)))


def display_load_factor(load_factor: float) -> float:
    """Clamp load factor for UI/ranking while raw values remain available in debug."""
    try:
        value = float(load_factor)
    except (TypeError, ValueError):
        return FALLBACK_LOAD_FACTOR
    return max(0.0, min(value, LOAD_FACTOR_OUTPUT_CAP))


def _service_headway(pattern: dict) -> int:
    return int(pattern.get("service_headway") or pattern["headway"])


def _time_band_for_seconds(seconds: int | float) -> dict:
    second_of_day = int(seconds) % (24 * 3600)
    for band in TIME_BANDS:
        start = int(band["start"])
        end = int(band["end"])
        if start < end and start <= second_of_day < end:
            return band
        if start > end and (second_of_day >= start or second_of_day < end):
            return band
    return TIME_BAND_BY_NAME["late_night"]


def kategori_kepadatan(load_factor: float) -> str:
    if load_factor < 0.50:
        return "sepi"
    if load_factor < 0.80:
        return "sedang"
    if load_factor < 1.00:
        return "padat"
    return "sangat_padat"


def label_kepadatan(load_factor: float) -> str:
    kategori = kategori_kepadatan(load_factor)
    if kategori == "sepi":
        return "Sepi"
    if kategori == "sedang":
        return "Sedang"
    return "Padat"


def _normalize_koridor_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _sort_key_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _build_ridership_indexes(
    rows: list[dict],
    trip_supply_per_koridor: dict[str, int],
) -> tuple[
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
    str | None,
    dict[str, str],
    dict[str, list[str]],
]:
    ridership: dict[tuple[str, str], float] = {}
    latest_date_per_koridor: dict[str, str] = {}
    dates_per_koridor: dict[str, set[str]] = defaultdict(set)
    all_dates: set[str] = set()

    for row in rows:
        kid = _normalize_koridor_id(row.get("koridor_id"))
        if kid not in SCOPED_KORIDOR:
            continue
        tanggal = _sort_key_date(row.get("tanggal"))
        all_dates.add(tanggal)
        dates_per_koridor[kid].add(tanggal)
        try:
            jumlah = float(row.get("jumlah_pelanggan_pemodelan") or 0)
        except (TypeError, ValueError):
            jumlah = 0.0
        ridership[(tanggal, kid)] = jumlah
        if kid not in latest_date_per_koridor or tanggal > latest_date_per_koridor[kid]:
            latest_date_per_koridor[kid] = tanggal

    latest_date = max(all_dates) if all_dates else None
    recent_dates_per_koridor = {
        kid: sorted(dates, reverse=True)[:RIDERSHIP_RANDOM_SAMPLE_SIZE]
        for kid, dates in dates_per_koridor.items()
    }
    daily_mean: dict[tuple[str, str], float] = {}
    for (tanggal, kid), jumlah in ridership.items():
        supply = trip_supply_per_koridor.get(kid, 0)
        if supply <= 0:
            print(
                f"[gtfs] WARNING: tidak ada GTFS frequency untuk koridor {kid}; "
                f"daily mean fallback {FALLBACK_LOAD_FACTOR}"
            )
            daily_mean[(tanggal, kid)] = FALLBACK_LOAD_FACTOR
        else:
            daily_mean[(tanggal, kid)] = jumlah / (supply * BUS_CAPACITY)
    return ridership, daily_mean, latest_date, latest_date_per_koridor, recent_dates_per_koridor


def _match_trip_segments(
    stops: list[dict],
    segmen_by_pair: dict[tuple[str, str, str], dict],
    segmen_by_koridor: dict[str, list[dict]],
    kid: str,
) -> tuple[list[str], int, bool, int, int, float]:
    segmen_ids: list[str] = []
    missing = 0
    total_pairs = max(0, len(stops) - 1)
    for i in range(len(stops) - 1):
        a = stops[i]["halte_id"]
        b = stops[i + 1]["halte_id"]
        seg = segmen_by_pair.get((kid, a, b))
        if seg is None:
            missing += 1
            continue
        segmen_ids.append(str(seg["segmen_id"]))
    used_fallback = False
    if not segmen_ids and len(stops) > 1:
        segmen_ids = [str(s["segmen_id"]) for s in segmen_by_koridor.get(kid, [])]
        used_fallback = bool(segmen_ids)
    matched_pairs = total_pairs - missing
    match_ratio = matched_pairs / total_pairs if total_pairs else 0.0
    return segmen_ids, missing, used_fallback, matched_pairs, total_pairs, match_ratio


def _build_template_stops(
    trip_id: str,
    template_rows: list[dict],
    halte_by_id: dict[str, dict],
    kid: str,
) -> list[dict] | None:
    if len(template_rows) < 2:
        print(f"[gtfs] WARNING: skip trip {trip_id}; stop sequence kurang dari 2")
        return None

    template_stops: list[dict] = []
    for row in template_rows:
        halte_id = str(row.get("stop_id"))
        halte = halte_by_id.get(halte_id)
        if halte is None:
            print(f"[gtfs] WARNING: skip trip {trip_id}; halte {halte_id} tidak punya koordinat")
            return None
        try:
            arr = parse_gtfs_time(row.get("arrival_time"))
            dep = parse_gtfs_time(row.get("departure_time"))
        except ValueError as e:
            print(f"[gtfs] WARNING: skip trip {trip_id}; stop time invalid: {e!r}")
            return None
        template_stops.append({
            "halte_id": halte_id,
            "nama_halte": halte.get("nama", halte_id),
            "lat": halte.get("lat"),
            "lng": halte.get("lng"),
            "arrival_template": arr,
            "departure_template": dep,
            "urutan": int(row.get("stop_sequence") or 0),
            "koridor_id": int(kid),
        })
    return template_stops


def _active_count_by_corridor_direction(instances: list[dict], sim_time: int) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for instance in instances:
        if _is_active(instance, sim_time):
            counts[(instance["koridor_key"], instance["direction_id"])] += 1
    return dict(counts)


def _active_count_by_trip_id(instances: list[dict], sim_time: int) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for instance in instances:
        if _is_active(instance, sim_time):
            counts[instance["trip_id"]] += 1
    return dict(counts)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    return distance_meters(lat1, lng1, lat2, lng2)


def _active_instance_state(instance: dict, sim_time: int) -> dict | None:
    if not _is_active(instance, sim_time):
        return None

    stops = instance["stops"]
    total_pairs = max(1, len(stops) - 1)
    for i in range(len(stops) - 1):
        a = stops[i]
        b = stops[i + 1]
        start = a.get("waktu_berangkat_detik", a["waktu_tiba_detik"])
        end = b["waktu_tiba_detik"]
        if start <= sim_time < end:
            denom = end - start
            pct = 0.0 if denom == 0 else (sim_time - start) / denom
            lat = a["lat"] + (b["lat"] - a["lat"]) * pct
            lng = a["lng"] + (b["lng"] - a["lng"]) * pct
            return {
                "route_id": instance["koridor_key"],
                "direction_id": instance["direction_id"],
                "trip_id": instance["trip_id"],
                "trip_instance_id": instance["trip_instance_id"],
                "departure_time": instance["departure_time"],
                "current_segment": f"{a['halte_id']}->{b['halte_id']}",
                "progress_pct": ((i + pct) / total_pairs) * 100,
                "lat": lat,
                "lng": lng,
            }

    # Dwell-time fallback: keep the marker at the latest reached stop.
    latest_index = 0
    for i, stop in enumerate(stops):
        if stop["waktu_tiba_detik"] <= sim_time:
            latest_index = i
    stop = stops[latest_index]
    next_stop = stops[min(latest_index + 1, len(stops) - 1)]
    return {
        "route_id": instance["koridor_key"],
        "direction_id": instance["direction_id"],
        "trip_id": instance["trip_id"],
        "trip_instance_id": instance["trip_instance_id"],
        "departure_time": instance["departure_time"],
        "current_segment": f"{stop['halte_id']}->{next_stop['halte_id']}",
        "progress_pct": (latest_index / total_pairs) * 100,
        "lat": stop["lat"],
        "lng": stop["lng"],
    }


def _active_count_for_pattern(pattern: dict, instances: list[dict], sim_time: int) -> int:
    return sum(
        1
        for instance in instances
        if instance["trip_id"] == pattern["trip_id"] and _is_active(instance, sim_time)
    )


def _print_spatial_spacing_audit(instances: list[dict]) -> None:
    for label, sim_time in AUDIT_SAMPLE_TIMES.items():
        states = [
            state
            for instance in instances
            if (state := _active_instance_state(instance, sim_time)) is not None
        ]
        by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for state in states:
            by_direction[(state["route_id"], state["direction_id"])].append(state)

        for (route_id, direction_id), group in sorted(by_direction.items()):
            group.sort(key=lambda s: (s["progress_pct"], s["departure_time"], s["trip_instance_id"]))
            progress_gaps: list[float] = []
            departure_gaps: list[float] = []
            distance_gaps: list[float] = []
            closest_pairs: list[dict] = []

            for prev, curr in zip(group, group[1:]):
                progress_gap = curr["progress_pct"] - prev["progress_pct"]
                departure_gap = abs(curr["departure_time"] - prev["departure_time"])
                distance_gap = _distance_meters(prev["lat"], prev["lng"], curr["lat"], curr["lng"])
                progress_gaps.append(progress_gap)
                departure_gaps.append(departure_gap)
                distance_gaps.append(distance_gap)
                closest_pairs.append({
                    "a": prev,
                    "b": curr,
                    "progress_gap": progress_gap,
                    "departure_gap": departure_gap,
                    "distance_gap": distance_gap,
                })

            close_progress_count = sum(1 for gap in progress_gaps if gap < 1.0)
            close_departure_count = sum(1 for gap in departure_gaps if gap < 120)
            print(
                "[gtfs_spatial_audit] spacing_summary "
                f"time={label} "
                f"route_id={route_id} "
                f"direction_id={direction_id} "
                f"active_count={len(group)} "
                f"min_progress_gap={min(progress_gaps) if progress_gaps else None} "
                f"median_progress_gap={_median(progress_gaps)} "
                f"min_departure_gap_seconds={min(departure_gaps) if departure_gaps else None} "
                f"median_departure_gap_seconds={_median(departure_gaps)} "
                f"min_distance_gap_meters={min(distance_gaps) if distance_gaps else None} "
                f"median_distance_gap_meters={_median(distance_gaps)} "
                f"pairs_progress_gap_lt_1pct={close_progress_count} "
                f"pairs_departure_gap_lt_120s={close_departure_count}"
            )

            for pair in sorted(closest_pairs, key=lambda p: (p["progress_gap"], p["departure_gap"]))[:5]:
                a = pair["a"]
                b = pair["b"]
                print(
                    "[gtfs_spatial_audit] closest_pair "
                    f"time={label} "
                    f"route_id={route_id} "
                    f"direction_id={direction_id} "
                    f"a_trip_id={a['trip_id']} "
                    f"a_trip_instance_id={a['trip_instance_id']} "
                    f"a_departure_time={a['departure_time']} "
                    f"a_segment={a['current_segment']} "
                    f"a_progress={a['progress_pct']:.3f} "
                    f"a_lat={a['lat']:.7f} "
                    f"a_lng={a['lng']:.7f} "
                    f"b_trip_id={b['trip_id']} "
                    f"b_trip_instance_id={b['trip_instance_id']} "
                    f"b_departure_time={b['departure_time']} "
                    f"b_segment={b['current_segment']} "
                    f"b_progress={b['progress_pct']:.3f} "
                    f"b_lat={b['lat']:.7f} "
                    f"b_lng={b['lng']:.7f} "
                    f"progress_diff_percentage={pair['progress_gap']:.3f} "
                    f"time_gap_seconds={pair['departure_gap']} "
                    f"distance_gap_meters={pair['distance_gap']:.1f}"
                )


def _ordered_overlap_ratio(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    if not a or not b:
        return 0.0
    previous = [0] * (len(b) + 1)
    for item_a in a:
        current = [0] * (len(b) + 1)
        for j, item_b in enumerate(b, start=1):
            if item_a == item_b:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous = current
    return previous[-1] / min(len(a), len(b))


def _trip_duration_seconds(pattern: dict) -> int:
    return (
        pattern["template_stops"][-1]["arrival_template"]
        - pattern["template_stops"][0]["departure_template"]
    )


def _select_representative_pattern(patterns: list[dict]) -> dict:
    return min(
        patterns,
        key=lambda p: (
            -p.get("match_ratio", 0.0),
            -len(p["template_stops"]),
            p["headway"],
            -_trip_duration_seconds(p),
            p["trip_id"],
        ),
    )


def _selection_reason(representative: dict, members: list[dict]) -> str:
    if len(members) == 1:
        return "only pattern in overlap group"
    best_match = max(p.get("match_ratio", 0.0) for p in members)
    tied_match = [p for p in members if p.get("match_ratio", 0.0) == best_match]
    if representative.get("match_ratio", 0.0) > min(p.get("match_ratio", 0.0) for p in members):
        return "selected because highest segment_match_ratio"

    best_stop_count = max(len(p["template_stops"]) for p in tied_match)
    tied_stop_count = [p for p in tied_match if len(p["template_stops"]) == best_stop_count]
    if len(representative["template_stops"]) > min(len(p["template_stops"]) for p in tied_match):
        return "selected because same match ratio but highest stop_count"

    best_headway = min(p["headway"] for p in tied_stop_count)
    tied_headway = [p for p in tied_stop_count if p["headway"] == best_headway]
    if representative["headway"] < max(p["headway"] for p in tied_stop_count):
        return "selected because same match ratio/stop_count but lower headway_secs"

    best_duration = max(_trip_duration_seconds(p) for p in tied_headway)
    if _trip_duration_seconds(representative) > min(_trip_duration_seconds(p) for p in tied_headway):
        return "selected because same match/headway but longest trip_duration_seconds"
    return "selected by deterministic trip_id tie-break"


def _pattern_relationship(a: dict, b: dict) -> dict:
    stop_count_a = len(a["stop_signature"])
    stop_count_b = len(b["stop_signature"])
    max_stop_count = max(stop_count_a, stop_count_b)
    stop_count_ratio = min(stop_count_a, stop_count_b) / max_stop_count if max_stop_count else 0.0
    overlap = _ordered_overlap_ratio(a["stop_signature"], b["stop_signature"])
    first_a = a["stop_signature"][0] if a["stop_signature"] else None
    last_a = a["stop_signature"][-1] if a["stop_signature"] else None
    first_b = b["stop_signature"][0] if b["stop_signature"] else None
    last_b = b["stop_signature"][-1] if b["stop_signature"] else None
    same_first_stop = first_a == first_b
    same_last_stop = last_a == last_b
    same_or_similar_endpoints = same_first_stop and same_last_stop

    if (
        overlap >= OVERLAP_GROUP_THRESHOLD
        and stop_count_ratio >= 0.80
        and same_or_similar_endpoints
    ):
        decision = "duplicate_mergeable"
    elif overlap >= OVERLAP_GROUP_THRESHOLD and stop_count_ratio < 0.80:
        decision = "kept_separate_as_possible_subroute"
    elif overlap >= OVERLAP_GROUP_THRESHOLD:
        decision = "kept_separate_different_endpoints"
    else:
        decision = "kept_separate_low_overlap"

    return {
        "trip_id_a": a["trip_id"],
        "trip_id_b": b["trip_id"],
        "ordered_stop_overlap": overlap,
        "stop_count_a": stop_count_a,
        "stop_count_b": stop_count_b,
        "stop_count_ratio": stop_count_ratio,
        "first_stop_a": first_a,
        "last_stop_a": last_a,
        "first_stop_b": first_b,
        "last_stop_b": last_b,
        "same_first_stop": same_first_stop,
        "same_last_stop": same_last_stop,
        "same_or_similar_endpoints": same_or_similar_endpoints,
        "decision": decision,
    }


def _merge_overlapping_patterns(patterns: list[dict]) -> tuple[list[dict], list[dict], dict[tuple[str, str], list[dict]]]:
    by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pattern in patterns:
        by_direction[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)

    selected: list[dict] = []
    groups: list[dict] = []
    relationships_by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for (route_id, direction_id), group_patterns in sorted(by_direction.items()):
        for i in range(len(group_patterns)):
            for j in range(i + 1, len(group_patterns)):
                relationships_by_direction[(route_id, direction_id)].append(
                    _pattern_relationship(group_patterns[i], group_patterns[j])
                )

        remaining = set(range(len(group_patterns)))
        while remaining:
            stack = [min(remaining)]
            component: set[int] = set()
            while stack:
                idx = stack.pop()
                if idx in component:
                    continue
                component.add(idx)
                for other_idx in list(remaining):
                    if other_idx == idx or other_idx in component:
                        continue
                    overlap = _ordered_overlap_ratio(
                        group_patterns[idx]["stop_signature"],
                        group_patterns[other_idx]["stop_signature"],
                    )
                    if overlap >= OVERLAP_GROUP_THRESHOLD:
                        stack.append(other_idx)

            remaining -= component
            members = [group_patterns[i] for i in sorted(component)]
            representative = _select_representative_pattern(members)
            member_headways = sorted({int(m["headway"]) for m in members if int(m["headway"]) > 0})
            representative["representative_headway"] = int(representative["headway"])
            representative["service_headway"] = min(member_headways) if member_headways else int(representative["headway"])
            representative["merged_member_headways"] = member_headways
            selected.append(representative)
            overlap_values: dict[str, float] = {}
            for member in members:
                if member is representative:
                    continue
                overlap_values[member["trip_id"]] = _ordered_overlap_ratio(
                    representative["stop_signature"],
                    member["stop_signature"],
                )
            groups.append({
                "route_id": route_id,
                "direction_id": direction_id,
                "representative": representative,
                "members": members,
                "discarded": [m for m in members if m is not representative],
                "overlap_values": overlap_values,
                "reason_selected": _selection_reason(representative, members),
            })
    return selected, groups, relationships_by_direction


def _active_counts_from_patterns(patterns: list[dict]) -> dict[str, int]:
    counts = {label: 0 for label in AUDIT_SAMPLE_TIMES}
    by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pattern in patterns:
        by_direction[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)

    for group_patterns in by_direction.values():
        group_patterns = sorted(
            group_patterns,
            key=lambda p: (p["start_time"], p["end_time"], _service_headway(p), p["trip_id"]),
        )
        group_count = len(group_patterns)
        for pattern_index, pattern in enumerate(group_patterns):
            pattern_offset = 0
            headway = _service_headway(pattern)
            if group_count > 1:
                pattern_offset = int(round(pattern_index * headway / group_count))
            duration = _trip_duration_seconds(pattern)
            for label, sim_time in AUDIT_SAMPLE_TIMES.items():
                departure = pattern["start_time"] + pattern_offset
                while departure < pattern["end_time"]:
                    if departure <= sim_time < departure + duration:
                        counts[label] += 1
                    departure += headway
    return counts


def _generated_departures_from_patterns(patterns: list[dict]) -> int:
    total = 0
    for pattern in patterns:
        headway = _service_headway(pattern)
        departure = pattern["start_time"]
        while departure < pattern["end_time"]:
            total += 1
            departure += headway
    return total


def _generated_departures_for_pattern(pattern: dict) -> int:
    total = 0
    headway = _service_headway(pattern)
    departure = pattern["start_time"]
    while departure < pattern["end_time"]:
        total += 1
        departure += headway
    return total


def _active_counts_after_by_direction(instances: list[dict]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for label, sim_time in AUDIT_SAMPLE_TIMES.items():
        result[label] = {
            f"{kid}/{direction_id}": count
            for (kid, direction_id), count in sorted(
                _active_count_by_corridor_direction(instances, sim_time).items()
            )
        }
    return result


def _print_overlap_sanity_warnings(
    patterns_before_merge: list[dict],
    selected_patterns: list[dict],
    overlap_groups: list[dict],
    before_counts: dict[str, int],
    after_counts: dict[str, int],
) -> None:
    by_direction_before: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_direction_selected: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pattern in patterns_before_merge:
        by_direction_before[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)
    for pattern in selected_patterns:
        by_direction_selected[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)

    for key, before_patterns in sorted(by_direction_before.items()):
        route_id, direction_id = key
        selected = by_direction_selected.get(key, [])
        if not selected:
            print(
                "[gtfs_overlap_validation][WARNING] zero_selected_representative_patterns "
                f"route_id={route_id} direction_id={direction_id}"
            )
        if before_patterns and not selected:
            print(
                "[gtfs_overlap_validation][WARNING] active_patterns_all_discarded "
                f"route_id={route_id} direction_id={direction_id}"
            )

        before_est = sum(_trip_duration_seconds(p) / p["headway"] for p in before_patterns if p["headway"])
        after_est = sum(_trip_duration_seconds(p) / _service_headway(p) for p in selected if _service_headway(p))
        if before_est > 0 and after_est / before_est < 0.20:
            print(
                "[gtfs_overlap_validation][WARNING] active_trip_instances_drop_gt_80pct_estimated "
                f"route_id={route_id} direction_id={direction_id} "
                f"before_estimated={before_est:.2f} after_estimated={after_est:.2f}"
            )

    for group in overlap_groups:
        representative = group["representative"]
        discarded = group["discarded"]
        if representative.get("match_ratio", 0.0) < 0.80:
            print(
                "[gtfs_overlap_validation][WARNING] selected_representative_low_segment_match_ratio "
                f"route_id={group['route_id']} direction_id={group['direction_id']} "
                f"trip_id={representative['trip_id']} "
                f"match_ratio={representative.get('match_ratio', 0.0):.3f}"
            )
        max_discarded_stop_count = max((len(p["template_stops"]) for p in discarded), default=0)
        if discarded and len(representative["template_stops"]) < max_discarded_stop_count * 0.75:
            print(
                "[gtfs_overlap_validation][WARNING] selected_representative_shorter_than_discarded "
                f"route_id={group['route_id']} direction_id={group['direction_id']} "
                f"representative_trip_id={representative['trip_id']} "
                f"representative_stop_count={len(representative['template_stops'])} "
                f"max_discarded_stop_count={max_discarded_stop_count}"
            )
        better_discarded = [
            p for p in discarded
            if p.get("match_ratio", 0.0) > representative.get("match_ratio", 0.0) + 0.05
        ]
        if better_discarded:
            print(
                "[gtfs_overlap_validation][WARNING] discarded_pattern_has_better_segment_match_ratio "
                f"route_id={group['route_id']} direction_id={group['direction_id']} "
                f"representative_trip_id={representative['trip_id']} "
                f"discarded_trip_ids={[p['trip_id'] for p in better_discarded]}"
            )

    for label, count in after_counts.items():
        if count < 40 or count > 180:
            print(
                "[gtfs_overlap_validation][WARNING] total_active_trip_instances_outside_sanity_range "
                f"time={label} active_trip_instances={count} expected_range=40..180"
            )


def _print_overlap_merge_summary(
    raw_frequency_rows: int,
    patterns_before_merge: list[dict],
    selected_patterns: list[dict],
    overlap_groups: list[dict],
    relationships_by_direction: dict[tuple[str, str], list[dict]],
    instances_after_merge: list[dict],
) -> None:
    before_counts = _active_counts_from_patterns(patterns_before_merge)
    after_counts = {
        label: sum(_active_count_by_corridor_direction(instances_after_merge, sim_time).values())
        for label, sim_time in AUDIT_SAMPLE_TIMES.items()
    }
    generated_before = _generated_departures_from_patterns(patterns_before_merge)
    discarded_count = len(patterns_before_merge) - len(selected_patterns)
    print(f"[gtfs_overlap_merge] raw_frequency_rows={raw_frequency_rows}")
    print(f"[gtfs_overlap_merge] unique_service_patterns_before_overlap_merge={len(patterns_before_merge)}")
    print(f"[gtfs_overlap_merge] overlap_groups_count={len(overlap_groups)}")
    print(f"[gtfs_overlap_merge] selected_representative_patterns_count={len(selected_patterns)}")
    print(f"[gtfs_overlap_merge] discarded_overlapping_patterns_count={discarded_count}")
    print(f"[gtfs_overlap_merge] generated_trip_instances_before_merge={generated_before}")
    print(f"[gtfs_overlap_merge] generated_trip_instances_after_overlap_merge={len(instances_after_merge)}")
    for label in AUDIT_SAMPLE_TIMES:
        print(
            "[gtfs_overlap_merge] active_count_comparison "
            f"time={label} "
            f"before={before_counts[label]} "
            f"after={after_counts[label]}"
        )

    patterns_by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    selected_by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    groups_by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pattern in patterns_before_merge:
        patterns_by_direction[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)
    for pattern in selected_patterns:
        selected_by_direction[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)
    for group in overlap_groups:
        groups_by_direction[(group["route_id"], group["direction_id"])].append(group)

    after_by_direction = _active_counts_after_by_direction(instances_after_merge)
    for key in sorted(patterns_by_direction):
        route_id, direction_id = key
        groups = groups_by_direction.get(key, [])
        selected = selected_by_direction.get(key, [])
        discarded = [p for g in groups for p in g["discarded"]]
        relationships = relationships_by_direction.get(key, [])
        possible_subroutes = [
            r for r in relationships
            if r["decision"] == "kept_separate_as_possible_subroute"
        ]
        low_overlap_variants = [
            r for r in relationships
            if r["decision"] == "kept_separate_low_overlap"
        ]
        singleton_selected = {g["representative"]["trip_id"] for g in groups if len(g["members"]) == 1}
        low_overlap_variant_trip_ids = [
            p["trip_id"]
            for p in selected
            if p["trip_id"] in singleton_selected and len(patterns_by_direction[key]) > 1
        ]
        active_samples = {
            label: after_by_direction[label].get(f"{route_id}/{direction_id}", 0)
            for label in AUDIT_SAMPLE_TIMES
        }
        print(
            "[gtfs_overlap_validation] route_direction "
            f"route_id={route_id} "
            f"direction_id={direction_id} "
            f"patterns_before_merge={len(patterns_by_direction[key])} "
            f"overlap_groups={len(groups)} "
            f"duplicate_merge_groups={sum(1 for g in groups if g['discarded'])} "
            f"possible_subroute_variants_detected={len(possible_subroutes)} "
            f"low_overlap_variants={len(low_overlap_variants)} "
            f"selected_representative_trip_ids={[p['trip_id'] for p in selected]} "
            f"discarded_trip_ids={[p['trip_id'] for p in discarded]} "
            f"kept_low_overlap_trip_ids={low_overlap_variant_trip_ids} "
            f"low_overlap_variants_preserved={bool(low_overlap_variant_trip_ids)} "
            f"active_trip_instances_after_merge_at_sample_times={active_samples}"
        )

        for relation in possible_subroutes:
            print(
                "[gtfs_overlap_validation] possible_subroute_variant "
                f"route_id={route_id} "
                f"direction_id={direction_id} "
                f"trip_id_a={relation['trip_id_a']} "
                f"trip_id_b={relation['trip_id_b']} "
                f"ordered_stop_overlap={relation['ordered_stop_overlap']:.3f} "
                f"stop_count_a={relation['stop_count_a']} "
                f"stop_count_b={relation['stop_count_b']} "
                f"stop_count_ratio={relation['stop_count_ratio']:.3f} "
                f"first_stop_a={relation['first_stop_a']} "
                f"last_stop_a={relation['last_stop_a']} "
                f"first_stop_b={relation['first_stop_b']} "
                f"last_stop_b={relation['last_stop_b']} "
                "decision=merged_by_aggressive_overlap_rule"
            )

    for group in overlap_groups:
        representative = group["representative"]
        discarded = group["discarded"]
        if not discarded:
            continue
        overlap_values = {
            trip_id: round(value, 3)
            for trip_id, value in sorted(group["overlap_values"].items())
        }
        print(
            "[gtfs_overlap_merge] group "
            f"route_id={group['route_id']} "
            f"direction_id={group['direction_id']} "
            f"representative_trip_id={representative['trip_id']} "
            f"discarded_trip_ids={[p['trip_id'] for p in discarded]} "
            f"ordered_stop_overlap_values={overlap_values} "
            f"representative_headway_secs={representative['headway']} "
            f"service_headway_secs={_service_headway(representative)} "
            f"merged_member_headways={representative.get('merged_member_headways', [])} "
            f"representative_stop_count={len(representative['template_stops'])} "
            f"representative_trip_duration_seconds={_trip_duration_seconds(representative)} "
            f"representative_match_ratio={representative.get('match_ratio', 0.0):.3f} "
            f"discarded_pattern_details={[{'trip_id': p['trip_id'], 'stop_count': len(p['template_stops']), 'headway_secs': p['headway'], 'trip_duration_seconds': _trip_duration_seconds(p), 'segment_match_ratio': round(p.get('match_ratio', 0.0), 3)} for p in discarded]} "
            f"reason_selected={group['reason_selected']}"
        )

    for pattern in selected_patterns:
        active_samples = {
            label: _active_count_for_pattern(pattern, instances_after_merge, sim_time)
            for label, sim_time in AUDIT_SAMPLE_TIMES.items()
        }
        duration = _trip_duration_seconds(pattern)
        service_headway = _service_headway(pattern)
        estimated_active = duration / service_headway if service_headway else 0.0
        print(
            "[gtfs_overlap_validation] selected_pattern_reasonableness "
            f"route_id={pattern['koridor_key']} "
            f"direction_id={pattern['direction_id']} "
            f"trip_id={pattern['trip_id']} "
            f"start_time={pattern['start_time']} "
            f"end_time={pattern['end_time']} "
            f"headway_secs={pattern['headway']} "
            f"service_headway_secs={service_headway} "
            f"trip_duration_seconds={duration} "
            f"estimated_active_instances={estimated_active:.2f} "
            f"active_instances_at_05_00={active_samples['05_00']} "
            f"active_instances_at_06_00={active_samples['06_00']} "
            f"active_instances_at_10_00={active_samples['10_00']} "
            f"active_instances_at_12_00={active_samples['12_00']} "
            f"active_instances_at_17_00={active_samples['17_00']} "
            f"active_instances_at_22_00={active_samples['22_00']}"
        )

    for label, counts in after_by_direction.items():
        print(f"[gtfs_overlap_validation] active_trip_instances_after_merge_by_route_direction_at_{label}={counts}")

    _print_overlap_sanity_warnings(
        patterns_before_merge,
        selected_patterns,
        overlap_groups,
        before_counts,
        after_counts,
    )


def _print_pattern_overlap_audit(patterns: list[dict], instances: list[dict]) -> None:
    patterns_by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pattern in patterns:
        patterns_by_direction[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)

    for (route_id, direction_id), group in sorted(patterns_by_direction.items()):
        trip_ids = [p["trip_id"] for p in group]
        print(
            "[gtfs_spatial_audit] active_trip_id_patterns "
            f"route_id={route_id} "
            f"direction_id={direction_id} "
            f"trip_ids={trip_ids}"
        )
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a = group[i]
                b = group[j]
                overlap = _ordered_overlap_ratio(a["stop_signature"], b["stop_signature"])
                if overlap <= 0.80:
                    continue
                active_a = {
                    label: _active_count_for_pattern(a, instances, sim_time)
                    for label, sim_time in AUDIT_SAMPLE_TIMES.items()
                }
                active_b = {
                    label: _active_count_for_pattern(b, instances, sim_time)
                    for label, sim_time in AUDIT_SAMPLE_TIMES.items()
                }
                print(
                    "[gtfs_spatial_audit] overlapping_patterns "
                    f"route_id={route_id} "
                    f"direction_id={direction_id} "
                    f"trip_id_a={a['trip_id']} "
                    f"trip_id_b={b['trip_id']} "
                    f"ordered_stop_overlap={overlap:.3f} "
                    f"active_instances_a={active_a} "
                    f"active_instances_b={active_b}"
                )


def _print_generation_summary(
    raw_trips_count: int,
    trips_with_block_id: int,
    unique_block_id_count: int,
    raw_frequency_rows: int,
    patterns: list[dict],
    instances: list[dict],
    stats: dict[tuple[str, str], dict[str, int]],
) -> None:
    active_counts = _active_count_by_corridor_direction(instances, DEFAULT_DEBUG_TIME)
    active_by_time = {
        label: sum(_active_count_by_corridor_direction(instances, sim_time).values())
        for label, sim_time in AUDIT_SAMPLE_TIMES.items()
    }
    active_by_trip_at_default = _active_count_by_trip_id(instances, DEFAULT_DEBUG_TIME)

    print("[gtfs_audit] terminology: counts below are active_trip_instances, not physical_active_buses")
    print("[gtfs_audit] physical_fleet_rotation_modeled=false")
    print(f"[gtfs] raw frequency rows: {raw_frequency_rows}")
    print(f"[gtfs] raw GTFS trips count: {raw_trips_count}")
    print(f"[gtfs] gtfs_trips with block_id: {trips_with_block_id}/{raw_trips_count}")
    print(f"[gtfs_audit] block_id_populated_count={trips_with_block_id}/{raw_trips_count}")
    print(f"[gtfs_audit] unique_block_id_count={unique_block_id_count}")
    print(
        "[gtfs_audit] block_rotation_feasibility_check="
        "not_modeled; requires ordered non-overlapping trips and stop compatibility validation"
    )
    print("[gtfs] physical fleet rotation modeled: false")
    print(f"[gtfs] unique service patterns: {len(patterns)}")
    print(f"[gtfs] generated simulated trip instances: {len(instances)}")
    print(f"[gtfs_audit] total_raw_gtfs_frequency_rows={raw_frequency_rows}")
    print(f"[gtfs_audit] unique_service_patterns={len(patterns)}")
    print(f"[gtfs_audit] generated_trip_instances_total={len(instances)}")
    print(f"[gtfs_audit] active_trip_instances_at_sample_times={active_by_time}")
    readable_active = {
        f"{kid}/{direction_id}": count
        for (kid, direction_id), count in sorted(active_counts.items())
    }
    print(f"[gtfs] raw active trip instances at 05:00: {sum(active_counts.values())}")
    print(f"[gtfs] active_trip_instances at 05:00 by route_id/direction_id: {readable_active}")
    print(f"[gtfs_audit] active_trip_instances_by_route_direction_at_05_00={readable_active}")
    print(f"[gtfs_audit] active_trip_instances_by_trip_id_at_05_00={dict(sorted(active_by_trip_at_default.items()))}")

    all_keys = sorted(set(stats.keys()) | set(active_counts.keys()))
    for kid, direction_id in all_keys:
        row = stats[(kid, direction_id)]
        print(
            "[gtfs] summary "
            f"koridor_id={kid} direction_id={direction_id} "
            f"raw_frequency_rows={row.get('raw_frequency_rows', 0)} "
            f"unique_patterns={row.get('unique_patterns', 0)} "
            f"generated_instances={row.get('generated_instances', 0)} "
            f"active_trip_instances_at_05:00={active_counts.get((kid, direction_id), 0)}"
        )

    for pattern in patterns:
        trip_duration = (
            pattern["template_stops"][-1]["arrival_template"]
            - pattern["template_stops"][0]["departure_template"]
        )
        service_headway = _service_headway(pattern)
        estimated_active = trip_duration / service_headway if service_headway else 0.0
        active_samples = {
            label: _active_count_for_pattern(pattern, instances, sim_time)
            for label, sim_time in AUDIT_SAMPLE_TIMES.items()
        }
        print(
            "[gtfs_audit] generated_service_pattern "
            f"route_id={pattern['koridor_key']} "
            f"direction_id={pattern['direction_id']} "
            f"trip_id={pattern['trip_id']} "
            f"block_id={pattern.get('block_id') or ''} "
            f"start_time={pattern['start_time']} "
            f"end_time={pattern['end_time']} "
            f"headway_secs={pattern['headway']} "
            f"service_headway_secs={service_headway} "
            f"stop_count={len(pattern['template_stops'])} "
            f"trip_duration_seconds={trip_duration} "
            f"generated_departures_count={pattern.get('generated_departures_count', _generated_departures_for_pattern(pattern))} "
            f"estimated_active_instances={estimated_active:.2f} "
            f"active_instances_at_05_00={active_samples['05_00']} "
            f"active_instances_at_06_00={active_samples['06_00']} "
            f"active_instances_at_12_00={active_samples['12_00']} "
            f"active_instances_at_17_00={active_samples['17_00']} "
            f"active_instances_at_22_00={active_samples['22_00']}"
        )

    patterns_by_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pattern in patterns:
        patterns_by_direction[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)

    for (kid, direction_id), group in sorted(patterns_by_direction.items()):
        ranked = sorted(
            group,
            key=lambda p: (
                -(
                    (p["template_stops"][-1]["arrival_template"] - p["template_stops"][0]["departure_template"])
                    / _service_headway(p)
                    if _service_headway(p)
                    else 0
                ),
                _service_headway(p),
                p["trip_id"],
            ),
        )
        for pattern in ranked[:10]:
            duration = pattern["template_stops"][-1]["arrival_template"] - pattern["template_stops"][0]["departure_template"]
            service_headway = _service_headway(pattern)
            estimated_active = duration / service_headway if service_headway else 0.0
            print(
                "[gtfs_audit] top_active_contributor "
                f"route_id={kid} "
                f"direction_id={direction_id} "
                f"trip_id={pattern['trip_id']} "
                f"headway_secs={pattern['headway']} "
                f"service_headway_secs={service_headway} "
                f"trip_duration_seconds={duration} "
                f"estimated_active_instances={estimated_active:.2f}"
            )

    _print_spatial_spacing_audit(instances)
    _print_pattern_overlap_audit(patterns, instances)


def _generate_instances(
    trips: list[dict],
    frequencies: list[dict],
    stop_times: list[dict],
    halte_by_id: dict[str, dict],
    segmen: list[dict],
    allowed_halte_ids: set[str] | None = None,
) -> tuple[list[dict], dict[str, list[dict]], dict[str, int]]:
    trips_by_id = {str(t["trip_id"]): t for t in trips}
    stop_times_by_trip: dict[str, list[dict]] = defaultdict(list)
    for row in stop_times:
        stop_times_by_trip[str(row["trip_id"])].append(row)

    segmen_by_pair: dict[tuple[str, str, str], dict] = {}
    segmen_by_koridor: dict[str, list[dict]] = defaultdict(list)
    for seg in segmen:
        kid = _normalize_koridor_id(seg.get("koridor_id"))
        a = str(seg.get("halte_asal"))
        b = str(seg.get("halte_tujuan"))
        if kid:
            segmen_by_koridor[kid].append(seg)
        if kid and a and b:
            segmen_by_pair[(kid, a, b)] = seg

    trip_supply_per_koridor: dict[str, int] = defaultdict(int)
    sequence_per_key: dict[tuple[str, str], int] = defaultdict(int)
    instances: list[dict] = []
    jadwal: dict[str, list[dict]] = {}
    patterns_by_key: dict[tuple, dict] = {}
    raw_frequency_rows = 0
    stats: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {
        "raw_frequency_rows": 0,
        "unique_patterns": 0,
        "generated_instances": 0,
    })

    for freq in frequencies:
        trip_id = str(freq.get("trip_id"))
        trip = trips_by_id.get(trip_id)
        if not trip:
            print(f"[gtfs] WARNING: frequency trip_id {trip_id} tidak ditemukan di gtfs_trips")
            continue
        kid = _normalize_koridor_id(trip.get("route_id"))
        if kid not in SCOPED_KORIDOR:
            continue
        raw_frequency_rows += 1

        try:
            start_time = parse_gtfs_time(freq.get("start_time"))
            end_time = parse_gtfs_time(freq.get("end_time"))
            headway = int(freq.get("headway_secs"))
        except (TypeError, ValueError) as e:
            print(f"[gtfs] WARNING: skip frequency invalid trip {trip_id}: {e!r}")
            continue
        if headway <= 0 or end_time <= start_time:
            print(f"[gtfs] WARNING: skip frequency invalid range/headway trip {trip_id}")
            continue

        direction_id = str(trip.get("direction_id", "0"))
        stats[(kid, direction_id)]["raw_frequency_rows"] += 1

        template_rows = sorted(
            stop_times_by_trip.get(trip_id, []),
            key=lambda r: int(r.get("stop_sequence") or 0),
        )
        if allowed_halte_ids is not None:
            template_rows = [
                row
                for row in template_rows
                if str(row.get("stop_id")) in allowed_halte_ids
            ]
        template_stops = _build_template_stops(trip_id, template_rows, halte_by_id, kid)
        if template_stops is None:
            continue

        stop_signature = tuple(stop["halte_id"] for stop in template_stops)
        pattern_key = (kid, direction_id, stop_signature, start_time, end_time, headway)
        if pattern_key in patterns_by_key:
            patterns_by_key[pattern_key]["raw_frequency_rows"] += 1
            continue

        (
            segment_ids,
            missing_pairs,
            used_segment_fallback,
            matched_pairs,
            total_pairs,
            match_ratio,
        ) = _match_trip_segments(
            template_stops,
            segmen_by_pair,
            segmen_by_koridor,
            kid,
        )
        if missing_pairs:
            print(
                f"[gtfs] corridor {kid} direction {direction_id}: "
                f"{missing_pairs} unmatched stop-pairs for pattern {trip_id}"
            )
        if used_segment_fallback:
            print(
                f"[gtfs] WARNING: corridor {kid} direction {direction_id} pattern {trip_id} "
                "fallback ke semua segmen koridor; arah bisa tercampur"
            )

        patterns_by_key[pattern_key] = {
            "trip_id": trip_id,
            "koridor_key": kid,
            "koridor_id": int(kid),
            "direction_id": direction_id,
            "trip_headsign": trip.get("trip_headsign"),
            "block_id": trip.get("block_id"),
            "stop_signature": stop_signature,
            "start_time": start_time,
            "end_time": end_time,
            "headway": headway,
            "template_stops": template_stops,
            "segment_ids": segment_ids,
            "matched_pairs": matched_pairs,
            "total_pairs": total_pairs,
            "match_ratio": match_ratio,
            "raw_frequency_rows": 1,
        }

    patterns = sorted(
        patterns_by_key.values(),
        key=lambda p: (
            p["koridor_key"],
            p["direction_id"],
            p["start_time"],
            p["end_time"],
            p["headway"],
            p["trip_id"],
        ),
    )

    selected_patterns, overlap_groups, relationships_by_direction = _merge_overlapping_patterns(patterns)

    patterns_by_corridor_direction: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pattern in selected_patterns:
        patterns_by_corridor_direction[(pattern["koridor_key"], pattern["direction_id"])].append(pattern)

    for key, group_patterns in patterns_by_corridor_direction.items():
        stats[key]["unique_patterns"] = len(group_patterns)
        group_patterns.sort(
            key=lambda p: (
                p["start_time"],
                p["end_time"],
                _service_headway(p),
                p["trip_id"],
            )
        )
        group_count = len(group_patterns)
        for pattern_index, pattern in enumerate(group_patterns):
            kid = pattern["koridor_key"]
            direction_id = pattern["direction_id"]
            headway = _service_headway(pattern)
            pattern_offset = 0
            if group_count > 1:
                pattern_offset = int(round(pattern_index * headway / group_count))

            first_departure_template = pattern["template_stops"][0]["departure_template"]
            departure = pattern["start_time"] + pattern_offset
            generated_departures_count = 0
            while departure < pattern["end_time"]:
                sequence_per_key[(kid, direction_id)] += 1
                seq = sequence_per_key[(kid, direction_id)]
                bus_id = f"BUS-{kid}-{direction_id}-{seq:03d}"
                offset = departure - first_departure_template
                shifted_stops: list[dict] = []
                for stop in pattern["template_stops"]:
                    arr = stop["arrival_template"] + offset
                    dep = stop["departure_template"] + offset
                    shifted_stops.append({
                        "halte_id": stop["halte_id"],
                        "nama_halte": stop["nama_halte"],
                        "lat": stop["lat"],
                        "lng": stop["lng"],
                        "waktu_tiba_detik": arr,
                        "waktu_berangkat_detik": dep,
                        "urutan": stop["urutan"],
                        "koridor_id": stop["koridor_id"],
                    })

                first_departure = shifted_stops[0]["waktu_berangkat_detik"]
                last_arrival = shifted_stops[-1]["waktu_tiba_detik"]
                if last_arrival <= first_departure:
                    print(f"[gtfs] WARNING: skip instance {bus_id}; durasi trip tidak valid")
                    departure += headway
                    continue

                trip_instance_id = bus_id
                instance = {
                    "bus_id": bus_id,
                    "trip_instance_id": trip_instance_id,
                    "trip_id": pattern["trip_id"],
                    "koridor_id": pattern["koridor_id"],
                    "koridor_key": kid,
                    "direction_id": direction_id,
                    "trip_headsign": pattern["trip_headsign"],
                    "departure_time": departure,
                    "first_stop_departure_time": first_departure,
                    "last_stop_arrival_time": last_arrival,
                    "stops": shifted_stops,
                    "segment_ids": pattern["segment_ids"],
                }
                instances.append(instance)
                jadwal[bus_id] = shifted_stops
                trip_supply_per_koridor[kid] += 1
                stats[(kid, direction_id)]["generated_instances"] += 1
                generated_departures_count += 1
                departure += headway
            pattern["generated_departures_count"] = generated_departures_count

    instances.sort(key=lambda i: (i["departure_time"], i["bus_id"]))
    _print_overlap_merge_summary(
        raw_frequency_rows,
        patterns,
        selected_patterns,
        overlap_groups,
        relationships_by_direction,
        instances,
    )
    unique_block_ids = {
        str(trip.get("block_id"))
        for trip in trips_by_id.values()
        if trip.get("block_id")
    }
    _print_generation_summary(
        raw_trips_count=len(trips_by_id),
        trips_with_block_id=sum(1 for trip in trips_by_id.values() if trip.get("block_id")),
        unique_block_id_count=len(unique_block_ids),
        raw_frequency_rows=raw_frequency_rows,
        patterns=selected_patterns,
        instances=instances,
        stats=stats,
    )
    return instances, jadwal, dict(trip_supply_per_koridor)


def load_simulation_context(
    supabase,
    halte_rows: list[dict],
    segmen: list[dict],
    fallback_jadwal: dict[str, list[dict]] | None = None,
    allowed_halte_ids: set[str] | None = None,
) -> SimulationContext:
    """Load GTFS/ridership data and generate static trip instances."""
    try:
        trips = _fetch_semua(
            supabase,
            "gtfs_trips",
            "trip_id, route_id, direction_id, trip_headsign, block_id",
        )
        frequencies = _fetch_semua(
            supabase,
            "gtfs_frequencies",
            "trip_id, start_time, end_time, headway_secs",
        )
        stop_times = _fetch_semua(
            supabase,
            "gtfs_stop_times",
            "trip_id, stop_id, arrival_time, departure_time, stop_sequence",
        )
        ridership_rows = _fetch_semua(
            supabase,
            "ridership_harian_turunan",
            "tanggal, koridor_id, jumlah_pelanggan_pemodelan",
        )
    except Exception as e:
        print(f"[gtfs] WARNING: gagal load tabel GTFS/ridership ({e!r}); pakai fallback lama")
        return SimulationContext([], fallback_jadwal or {}, {}, {}, None, {}, {}, {}, {}, fallback_jadwal or {})

    halte_by_id = {str(h["halte_id"]): h for h in halte_rows}
    instances, jadwal, trip_supply = _generate_instances(
        trips, frequencies, stop_times, halte_by_id, segmen, allowed_halte_ids
    )
    (
        ridership,
        daily_mean,
        latest_date,
        latest_per_koridor,
        recent_dates_per_koridor,
    ) = _build_ridership_indexes(
        ridership_rows, trip_supply
    )
    print(
        f"[gtfs] generated {len(instances)} simulated trip instances "
        f"across {len(trip_supply)} corridors; latest ridership date={latest_date}"
    )
    return SimulationContext(
        instances=instances,
        jadwal=jadwal,
        trip_supply_per_koridor=trip_supply,
        daily_mean_load_factor=daily_mean,
        latest_date=latest_date,
        latest_date_per_koridor=latest_per_koridor,
        recent_dates_per_koridor=recent_dates_per_koridor,
        ridership_by_date_koridor=ridership,
        segmen_by_id={str(s["segmen_id"]): s for s in segmen},
        fallback_jadwal=fallback_jadwal or {},
    )


def resolve_simulation_run_id(value: str | None = None) -> str:
    return value or PROCESS_SIMULATION_RUN_ID


def resolve_ridership_date(ctx: SimulationContext, tanggal: str | None = None) -> str | None:
    return tanggal or ctx.latest_date


def _sample_ridership_date_for_koridor(
    ctx: SimulationContext,
    kid: str,
    simulation_run_id: str | None = None,
) -> str | None:
    candidates = ctx.recent_dates_per_koridor.get(kid, [])
    if not candidates:
        return ctx.latest_date_per_koridor.get(kid)
    run_id = resolve_simulation_run_id(simulation_run_id)
    idx = _stable_int_seed("ridership_sample_30", run_id, kid) % len(candidates)
    return candidates[idx]


def _ridership_for(
    ctx: SimulationContext,
    tanggal: str | None,
    kid: str,
    simulation_run_id: str | None = None,
) -> tuple[float | None, str | None]:
    if tanggal and (tanggal, kid) in ctx.ridership_by_date_koridor:
        return ctx.ridership_by_date_koridor[(tanggal, kid)], tanggal
    fallback_date = (
        ctx.latest_date_per_koridor.get(kid)
        if tanggal
        else _sample_ridership_date_for_koridor(ctx, kid, simulation_run_id)
    )
    if fallback_date and (fallback_date, kid) in ctx.ridership_by_date_koridor:
        if tanggal and fallback_date != tanggal:
            print(
                f"[gtfs] WARNING: ridership tanggal {tanggal} tidak ada untuk koridor {kid}; "
                f"pakai {fallback_date}"
            )
        return ctx.ridership_by_date_koridor[(fallback_date, kid)], fallback_date
    print(f"[gtfs] WARNING: ridership tidak ada untuk koridor {kid}; fallback {FALLBACK_LOAD_FACTOR}")
    return None, tanggal


def daily_mean_for(
    ctx: SimulationContext,
    tanggal: str | None,
    koridor_id: Any,
    simulation_run_id: str | None = None,
) -> float:
    kid = _normalize_koridor_id(koridor_id)
    jumlah, used_date = _ridership_for(ctx, tanggal, kid, simulation_run_id)
    if jumlah is None:
        return FALLBACK_LOAD_FACTOR
    supply = ctx.trip_supply_per_koridor.get(kid, 0)
    if supply <= 0:
        print(f"[gtfs] WARNING: trip supply kosong koridor {kid}; fallback {FALLBACK_LOAD_FACTOR}")
        return FALLBACK_LOAD_FACTOR
    return display_load_factor(jumlah / (supply * BUS_CAPACITY))


def _empty_time_band_summary() -> dict[str, dict]:
    return {
        str(band["name"]): {
            "time_band": band["name"],
            "time_range": band["time_range"],
            "time_band_weight": band["weight"],
            "trip_count": 0,
            "raw_weight_sum": 0.0,
            "allocated_passengers": 0.0,
            "average_trip_load_factor": 0.0,
            "min_trip_load_factor": 0.0,
            "max_trip_load_factor": 0.0,
            "_load_factors": [],
        }
        for band in TIME_BANDS
    }


def _finalize_time_band_summary(summary: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for band in TIME_BANDS:
        name = str(band["name"])
        row = summary[name]
        load_factors = row.pop("_load_factors", [])
        if load_factors:
            row["average_trip_load_factor"] = sum(load_factors) / len(load_factors)
            row["min_trip_load_factor"] = min(load_factors)
            row["max_trip_load_factor"] = max(load_factors)
        rows.append(row)
    return rows


def _print_crowding_debug(debug: dict) -> None:
    tolerance = 1e-6
    ok = abs(debug["allocation_error"]) <= tolerance
    print(
        "[gtfs_crowding] "
        f"tanggal={debug['tanggal']} "
        f"koridor_id={debug['koridor_id']} "
        f"jumlah_pelanggan_pemodelan={debug['jumlah_pelanggan_pemodelan']:.6f} "
        f"total_generated_trip_instances={debug['total_generated_trip_instances']} "
        f"total_allocated_passengers={debug['total_allocated_passengers']:.6f} "
        f"allocation_error={debug['allocation_error']:.12f}"
    )
    print(f"[gtfs_crowding] sanity sum_allocated_equals_daily_ridership={ok}")
    for row in debug["time_band_summary"]:
        print(
            "[gtfs_crowding] time_band_summary "
            f"time_band={row['time_band']} "
            f"time_range={row['time_range']} "
            f"time_band_weight={row['time_band_weight']:.2f} "
            f"trip_count={row['trip_count']} "
            f"raw_weight_sum={row['raw_weight_sum']:.6f} "
            f"allocated_passengers={row['allocated_passengers']:.6f} "
            f"average_trip_load_factor={row['average_trip_load_factor']:.6f} "
            f"min_trip_load_factor={row['min_trip_load_factor']:.6f} "
            f"max_trip_load_factor={row['max_trip_load_factor']:.6f}"
        )


def _trip_loads_for_corridor(
    ctx: SimulationContext,
    tanggal: str | None,
    koridor_key: str,
    simulation_run_id: str,
) -> tuple[dict[str, dict], dict | None]:
    corridor_instances = [i for i in ctx.instances if i["koridor_key"] == koridor_key]
    if not corridor_instances:
        return {}, None

    jumlah, used_date = _ridership_for(ctx, tanggal, koridor_key, simulation_run_id)
    if jumlah is None:
        return {
            i["trip_instance_id"]: {
                "trip_load_factor": FALLBACK_LOAD_FACTOR,
                "estimated_passengers": FALLBACK_LOAD_FACTOR * BUS_CAPACITY,
            }
            for i in corridor_instances
        }, None

    weights: dict[str, float] = {}
    time_bands_by_trip: dict[str, dict] = {}
    summary = _empty_time_band_summary()
    for instance in corridor_instances:
        seed = _stable_int_seed(
            used_date,
            koridor_key,
            instance["trip_instance_id"],
            simulation_run_id,
        )
        band = _time_band_for_seconds(instance["departure_time"])
        factor = _clamped_normal(seed, 1.0, 0.15, 0.60, 1.40)
        raw_weight = float(band["weight"]) * factor
        tid = instance["trip_instance_id"]
        weights[tid] = raw_weight
        time_bands_by_trip[tid] = band
        band_summary = summary[str(band["name"])]
        band_summary["trip_count"] += 1
        band_summary["raw_weight_sum"] += raw_weight

    total_weight = sum(weights.values()) or 1.0
    result: dict[str, dict] = {}
    for instance in corridor_instances:
        tid = instance["trip_instance_id"]
        passenger = jumlah * weights[tid] / total_weight
        trip_load_factor = passenger / BUS_CAPACITY
        result[tid] = {
            "trip_load_factor": trip_load_factor,
            "estimated_passengers": passenger,
            "tanggal": used_date,
            "time_band": time_bands_by_trip[tid]["name"],
            "time_band_weight": time_bands_by_trip[tid]["weight"],
            "raw_weight": weights[tid],
        }
        band_summary = summary[str(time_bands_by_trip[tid]["name"])]
        band_summary["allocated_passengers"] += passenger
        band_summary["_load_factors"].append(trip_load_factor)

    total_allocated = sum(payload["estimated_passengers"] for payload in result.values())
    debug = {
        "tanggal": used_date,
        "koridor_id": koridor_key,
        "jumlah_pelanggan_pemodelan": jumlah,
        "total_generated_trip_instances": len(corridor_instances),
        "total_allocated_passengers": total_allocated,
        "allocation_error": total_allocated - jumlah,
        "time_band_summary": _finalize_time_band_summary(summary),
    }
    return result, debug


def generate_crowding(
    ctx: SimulationContext,
    tanggal: str | None = None,
    simulation_run_id: str | None = None,
) -> dict:
    """Generate deterministic trip and segment load factors for one date/run."""
    run_id = resolve_simulation_run_id(simulation_run_id)
    resolved_date = resolve_ridership_date(ctx, tanggal)
    cache_date_key = resolved_date if tanggal else f"sample30:{run_id}"
    cache_key = (cache_date_key, run_id)
    if cache_key in ctx.crowding_cache:
        return ctx.crowding_cache[cache_key]

    trip_loads: dict[str, dict] = {}
    sampled_dates_by_koridor: dict[str, str | None] = {}
    for kid in SCOPED_KORIDOR:
        corridor_trip_loads, debug = _trip_loads_for_corridor(ctx, tanggal, kid, run_id)
        trip_loads.update(corridor_trip_loads)
        if debug is not None:
            sampled_dates_by_koridor[kid] = debug.get("tanggal")
            _print_crowding_debug(debug)

    segment_loads: dict[str, dict[str, float]] = {}
    for instance in ctx.instances:
        tid = instance["trip_instance_id"]
        load_payload = trip_loads.get(tid, {})
        base = load_payload.get("trip_load_factor", FALLBACK_LOAD_FACTOR)
        seed_date = load_payload.get("tanggal") or resolved_date
        seg_ids = instance.get("segment_ids") or []
        if not seg_ids:
            segment_loads[tid] = {}
            continue

        raw: dict[str, float] = {}
        for segmen_id in seg_ids:
            seed = _stable_int_seed(seed_date, instance["koridor_key"], tid, segmen_id, run_id)
            factor = _clamped_normal(seed, 1.0, 0.10, 0.80, 1.20)
            raw[segmen_id] = base * factor

        avg_raw = sum(raw.values()) / len(raw) if raw else base
        scale = base / avg_raw if avg_raw > 0 else 1.0
        segment_loads[tid] = {sid: value * scale for sid, value in raw.items()}

    result = {
        "tanggal": resolved_date if tanggal else None,
        "sampled_dates_by_koridor": sampled_dates_by_koridor,
        "simulation_run_id": run_id,
        "trip_loads": trip_loads,
        "segment_loads": segment_loads,
    }
    ctx.crowding_cache[cache_key] = result
    return result


def _is_active(instance: dict, sim_time: int) -> bool:
    return instance["first_stop_departure_time"] <= sim_time < instance["last_stop_arrival_time"]


def _current_segment_id(instance: dict, sim_time: int) -> str | None:
    stops = instance["stops"]
    segment_ids = instance.get("segment_ids") or []
    for i in range(len(stops) - 1):
        if i >= len(segment_ids):
            return None
        start = stops[i].get("waktu_berangkat_detik", stops[i]["waktu_tiba_detik"])
        end = stops[i + 1]["waktu_tiba_detik"]
        if start <= sim_time < end:
            return segment_ids[i]
    return None


def active_segment_crowding_snapshot(
    ctx: SimulationContext,
    sim_time: int,
    tanggal: str | None = None,
    simulation_run_id: str | None = None,
) -> dict[str, float]:
    crowding = generate_crowding(ctx, tanggal, simulation_run_id)
    values: dict[str, list[float]] = defaultdict(list)
    for instance in ctx.instances:
        if not _is_active(instance, sim_time):
            continue
        segmen_id = _current_segment_id(instance, sim_time)
        if segmen_id is None:
            continue
        load = crowding["segment_loads"].get(instance["trip_instance_id"], {}).get(segmen_id)
        if load is not None:
            values[segmen_id].append(display_load_factor(load))
    return {sid: display_load_factor(sum(loads) / len(loads)) for sid, loads in values.items()}


def realtime_trip_loads(
    ctx: SimulationContext,
    tanggal: str | None = None,
    simulation_run_id: str | None = None,
) -> dict[str, float]:
    crowding = generate_crowding(ctx, tanggal, simulation_run_id)
    return {
        tid: display_load_factor(payload.get("trip_load_factor", FALLBACK_LOAD_FACTOR))
        for tid, payload in crowding["trip_loads"].items()
    }


def get_active_positions(
    ctx: SimulationContext,
    sim_time: int,
    tanggal: str | None = None,
    simulation_run_id: str | None = None,
    max_visible_per_corridor_direction: int | None = None,
) -> list[dict]:
    crowding = generate_crowding(ctx, tanggal, simulation_run_id)
    positions: list[dict] = []
    for instance in ctx.instances:
        pos = get_bus_position(instance["bus_id"], instance["stops"], sim_time)
        if pos is None:
            continue
        payload = crowding["trip_loads"].get(instance["trip_instance_id"], {})
        raw_load = float(payload.get("trip_load_factor", FALLBACK_LOAD_FACTOR))
        load = display_load_factor(raw_load)
        pos.update({
            "trip_id": instance["trip_id"],
            "trip_instance_id": instance["trip_instance_id"],
            "direction_id": instance["direction_id"],
            "trip_load_factor": round(load, 3),
            "raw_trip_load_factor": round(raw_load, 3),
            "label_kepadatan": label_kepadatan(load),
            "kategori_kepadatan": kategori_kepadatan(load),
            "status": "active",
            "estimated_passengers": round(load * BUS_CAPACITY, 2),
            "raw_estimated_passengers": round(payload.get("estimated_passengers", raw_load * BUS_CAPACITY), 2),
            "capacity": BUS_CAPACITY,
        })
        positions.append(pos)

    limit = (
        max_visible_per_corridor_direction
        if max_visible_per_corridor_direction is not None
        else MAX_VISIBLE_BUSES_PER_CORRIDOR_DIRECTION
    )
    if limit and limit > 0:
        grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
        for pos in positions:
            grouped[(pos["koridor_id"], pos["direction_id"])].append(pos)
        positions = []
        for key in sorted(grouped):
            group = sorted(
                grouped[key],
                key=lambda p: (p["eta_minutes"], p["trip_instance_id"]),
            )
            positions.extend(group[:limit])
    return positions


def upcoming_buses_for_halte(
    ctx: SimulationContext,
    halte_id: str,
    sim_time: int,
    tanggal: str | None = None,
    simulation_run_id: str | None = None,
    limit: int = 500,
    max_eta_detik: int = MAX_ETA_DETIK_DEFAULT,
) -> list[dict]:
    crowding = generate_crowding(ctx, tanggal, simulation_run_id)
    candidates: list[dict] = []
    for instance in ctx.instances:
        for stop in instance["stops"]:
            if stop["halte_id"] != halte_id:
                continue
            arrival = stop["waktu_tiba_detik"]
            if arrival < sim_time:
                continue
            eta_detik = arrival - sim_time
            if eta_detik > max_eta_detik:
                continue
            load_payload = crowding["trip_loads"].get(instance["trip_instance_id"], {})
            raw_load = float(load_payload.get("trip_load_factor", FALLBACK_LOAD_FACTOR))
            load = display_load_factor(raw_load)
            candidates.append({
                "bus_id": instance["bus_id"],
                "trip_id": instance["trip_id"],
                "trip_instance_id": instance["trip_instance_id"],
                "koridor_id": instance["koridor_id"],
                "eta_detik": eta_detik,
                "eta_menit": round(eta_detik / 60),
                "trip_load_factor": round(load, 3),
                "raw_trip_load_factor": round(raw_load, 3),
                "label_kepadatan": label_kepadatan(load),
                "kategori_kepadatan": kategori_kepadatan(load),
                "estimated_passengers": round(load * BUS_CAPACITY, 2),
                "raw_estimated_passengers": round(
                    load_payload.get("estimated_passengers", raw_load * BUS_CAPACITY), 2
                ),
                "capacity": BUS_CAPACITY,
            })
            break
    candidates.sort(key=lambda x: x["eta_detik"])

    per_koridor: dict[int, dict] = {}
    for row in candidates:
        if row["koridor_id"] in per_koridor:
            continue
        per_koridor[row["koridor_id"]] = row
        if len(per_koridor) >= limit:
            break
    return sorted(per_koridor.values(), key=lambda x: x["eta_detik"])
