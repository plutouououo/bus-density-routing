"""Monte Carlo helpers for trip load-factor and routing sensitivity.

This module layers replication and aggregation on top of the existing GTFS
simulation and routing code. The deterministic baseline remains available in
services/gtfs_simulation.py; this module is the stochastic experiment path.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from itertools import combinations
from statistics import mean, stdev
from typing import Any

from services.bus_selector import select_bus_per_segmen
from services.dijkstra import KANDIDAT_RUTE_DEFAULT, build_graph, dijkstra, format_rute
from services.gtfs_simulation import (
    BUS_CAPACITY,
    FALLBACK_LOAD_FACTOR,
    SCOPED_KORIDOR,
    SimulationContext,
    _current_segment_id,
    _is_active,
    _normalize_koridor_id,
    _ridership_for,
    _stable_int_seed,
    _time_band_for_seconds,
    daily_mean_for,
    display_load_factor,
    resolve_ridership_date,
)

MC_DEFAULT_REPLICATIONS = 100
MC_POISSON_SCALE_FACTOR = 25
MC_SEGMENT_POISSON_LAMBDA = 1.0


def _coerce_master_seed(master_seed: int | str | None, *parts: Any) -> str:
    if master_seed is not None:
        return str(master_seed)
    return str(_stable_int_seed("monte_carlo_master_seed", *parts))


def build_request_master_seed(*parts: Any) -> str:
    """Build a deterministic seed from request-scoped parameters."""
    return _coerce_master_seed(None, *parts)


def _poisson_draw(seed: int, lam: float) -> int:
    lam = float(lam)
    if lam <= 0.0:
        return 0

    rng = random.Random(seed)
    if lam < 30.0:
        threshold = math.exp(-lam)
        product = 1.0
        value = 0
        while product > threshold:
            value += 1
            product *= rng.random()
        return value - 1

    return max(0, round(rng.normalvariate(lam, math.sqrt(lam))))


def _scaled_poisson_draw(seed: int, lam: float) -> tuple[int, float]:
    raw_draw = _poisson_draw(seed, float(lam) * MC_POISSON_SCALE_FACTOR)
    return raw_draw, raw_draw / MC_POISSON_SCALE_FACTOR


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _series_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "mean": 0.0,
            "std_dev": 0.0,
            "p05": 0.0,
            "p95": 0.0,
            "count": 0,
            "min": 0.0,
            "max": 0.0,
        }

    std_dev = stdev(values) if len(values) >= 2 else 0.0
    return {
        "mean": float(mean(values)),
        "std_dev": float(std_dev),
        "p05": _percentile(values, 0.05),
        "p95": _percentile(values, 0.95),
        "count": len(values),
        "min": float(min(values)),
        "max": float(max(values)),
    }


def _replication_seed(master_seed: str, replication_index: int) -> int:
    return _stable_int_seed(master_seed, replication_index)


def _replication_trip_loads_for_corridor(
    ctx: SimulationContext,
    tanggal: str | None,
    kid: str,
    replication_seed: int,
) -> tuple[dict[str, dict], dict | None]:
    corridor_instances = [
        instance
        for instance in ctx.instances
        if _normalize_koridor_id(instance.get("koridor_key")) == kid
    ]
    if not corridor_instances:
        return {}, None

    jumlah, used_date = _ridership_for(ctx, tanggal, kid, str(replication_seed))
    if jumlah is None:
        return {
            instance["trip_instance_id"]: {
                "trip_load_factor": FALLBACK_LOAD_FACTOR,
                "estimated_passengers": FALLBACK_LOAD_FACTOR * BUS_CAPACITY,
            }
            for instance in corridor_instances
        }, None

    weights: dict[str, float] = {}
    time_bands_by_trip: dict[str, dict] = {}
    for instance in corridor_instances:
        trip_id = instance["trip_instance_id"]
        band = _time_band_for_seconds(instance["departure_time"])
        band_lambda = float(band["weight"])
        trip_seed = _stable_int_seed(replication_seed, kid, trip_id, "trip")
        trip_draw_raw, raw_weight = _scaled_poisson_draw(trip_seed, band_lambda)
        weights[trip_id] = raw_weight
        time_bands_by_trip[trip_id] = band

    if sum(weights.values()) <= 0:
        weights = {
            instance["trip_instance_id"]: float(_time_band_for_seconds(instance["departure_time"])["weight"])
            for instance in corridor_instances
        }

    total_weight = sum(weights.values()) or 1.0
    result: dict[str, dict] = {}
    for instance in corridor_instances:
        trip_id = instance["trip_instance_id"]
        passenger = jumlah * weights[trip_id] / total_weight
        trip_load_factor = passenger / BUS_CAPACITY
        result[trip_id] = {
            "trip_load_factor": trip_load_factor,
            "estimated_passengers": passenger,
            "tanggal": used_date,
            "time_band": time_bands_by_trip[trip_id]["name"],
            "time_band_weight": time_bands_by_trip[trip_id]["weight"],
            "poisson_lambda": time_bands_by_trip[trip_id]["weight"],
            "poisson_draw_raw": trip_draw_raw,
            "poisson_scale_factor": MC_POISSON_SCALE_FACTOR,
            "raw_weight": weights[trip_id],
        }
    return result, {"tanggal": used_date}


def _replication_segment_loads(
    ctx: SimulationContext,
    trip_loads: dict[str, dict],
    replication_seed: int,
    diagnostic_segment_ids: set[str] | None = None,
    sim_time: int | None = None,
) -> tuple[dict[str, dict[str, float]], list[dict]]:
    segment_loads: dict[str, dict[str, float]] = {}
    segment_debug: list[dict] = []
    for instance in ctx.instances:
        trip_id = instance["trip_instance_id"]
        payload = trip_loads.get(trip_id, {})
        base = float(payload.get("trip_load_factor", FALLBACK_LOAD_FACTOR))
        segment_ids = instance.get("segment_ids") or []
        if not segment_ids:
            segment_loads[trip_id] = {}
            continue

        raw: dict[str, float] = {}
        draw_by_segment: dict[str, float] = {}
        raw_draw_by_segment: dict[str, int] = {}
        lambda_by_segment: dict[str, float] = {}
        for segment_id in segment_ids:
            segment_seed = _stable_int_seed(
                replication_seed,
                instance["koridor_key"],
                trip_id,
                segment_id,
                "segment",
            )
            draw_raw, factor = _scaled_poisson_draw(
                segment_seed,
                MC_SEGMENT_POISSON_LAMBDA,
            )
            draw_by_segment[segment_id] = factor
            raw_draw_by_segment[segment_id] = draw_raw
            lambda_by_segment[segment_id] = MC_SEGMENT_POISSON_LAMBDA
            raw[segment_id] = base * float(factor)

        if sum(raw.values()) <= 0:
            raw = {segment_id: base for segment_id in segment_ids}

        avg_raw = sum(raw.values()) / len(raw) if raw else base
        scale = base / avg_raw if avg_raw > 0 else 1.0
        segment_loads[trip_id] = {segment_id: value * scale for segment_id, value in raw.items()}
        if diagnostic_segment_ids:
            for segment_id in segment_ids:
                if segment_id not in diagnostic_segment_ids:
                    continue
                active = (
                    sim_time is not None
                    and _is_active(instance, sim_time)
                    and _current_segment_id(instance, sim_time) == segment_id
                )
                segment_debug.append({
                    "trip_instance_id": trip_id,
                    "segment_id": segment_id,
                    "base_trip_load_factor": base,
                    "trip_poisson_lambda": payload.get("poisson_lambda"),
                    "trip_poisson_draw_raw": payload.get("poisson_draw_raw"),
                    "trip_poisson_scale_factor": payload.get("poisson_scale_factor"),
                    "trip_scaled_weight": payload.get("raw_weight"),
                    "lambda": lambda_by_segment[segment_id],
                    "poisson_draw": draw_by_segment[segment_id],
                    "poisson_draw_raw": raw_draw_by_segment[segment_id],
                    "poisson_scale_factor": MC_POISSON_SCALE_FACTOR,
                    "pre_normalization_raw_weight": raw[segment_id],
                    "average_pre_normalization_raw_weight": avg_raw,
                    "normalization_scale": scale,
                    "final_segment_load_factor": segment_loads[trip_id][segment_id],
                    "active_at_sim_time": active,
                })
    return segment_loads, segment_debug


def simulate_load_factor_replication(
    ctx: SimulationContext,
    tanggal: str | None,
    replication_index: int,
    master_seed: str,
    diagnostic_segment_ids: set[str] | None = None,
    sim_time: int | None = None,
) -> dict:
    replication_seed = _replication_seed(master_seed, replication_index)
    trip_loads: dict[str, dict] = {}
    sampled_dates_by_koridor: dict[str, str | None] = {}

    for kid in sorted(SCOPED_KORIDOR):
        corridor_trip_loads, debug = _replication_trip_loads_for_corridor(
            ctx,
            tanggal,
            kid,
            replication_seed,
        )
        trip_loads.update(corridor_trip_loads)
        if debug is not None:
            sampled_dates_by_koridor[kid] = debug.get("tanggal")

    segment_loads, segment_debug = _replication_segment_loads(
        ctx,
        trip_loads,
        replication_seed,
        diagnostic_segment_ids=diagnostic_segment_ids,
        sim_time=sim_time,
    )
    total_allocated_passengers = sum(
        payload.get("estimated_passengers", 0.0) for payload in trip_loads.values()
    )

    replication = {
        "replication_index": replication_index,
        "master_seed": master_seed,
        "replication_seed": replication_seed,
        "tanggal": resolve_ridership_date(ctx, tanggal),
        "sampled_dates_by_koridor": sampled_dates_by_koridor,
        "trip_loads": trip_loads,
        "segment_loads": segment_loads,
        "segment_debug": segment_debug,
        "trip_load_factor_total": sum(
            float(payload.get("trip_load_factor", 0.0)) for payload in trip_loads.values()
        ),
        "allocated_passenger_total": total_allocated_passengers,
    }
    return replication


def aggregate_load_factor_replications(replications: list[dict]) -> dict:
    trip_series: dict[str, list[float]] = defaultdict(list)
    segment_series: dict[str, list[float]] = defaultdict(list)

    for replication in replications:
        for trip_id, payload in replication.get("trip_loads", {}).items():
            trip_series[trip_id].append(float(payload.get("trip_load_factor", 0.0)))
        for segment_map in replication.get("segment_loads", {}).values():
            for segment_id, value in segment_map.items():
                segment_series[segment_id].append(float(value))

    return {
        "trip_aggregates": {
            trip_id: _series_stats(values)
            for trip_id, values in sorted(trip_series.items())
        },
        "segment_aggregates": {
            segment_id: _series_stats(values)
            for segment_id, values in sorted(segment_series.items())
        },
        "full_day_segment_aggregates": {
            segment_id: _series_stats(values)
            for segment_id, values in sorted(segment_series.items())
        },
    }


def run_load_factor_monte_carlo(
    ctx: SimulationContext,
    tanggal: str | None = None,
    replications: int = MC_DEFAULT_REPLICATIONS,
    master_seed: int | str | None = None,
    diagnostic_segment_ids: set[str] | None = None,
    sim_time: int | None = None,
) -> dict:
    resolved_date = resolve_ridership_date(ctx, tanggal)
    seed_text = _coerce_master_seed(
        master_seed,
        resolved_date,
        replications,
        len(ctx.instances),
    )

    replication_rows = [
        simulate_load_factor_replication(
            ctx,
            resolved_date,
            index,
            seed_text,
            diagnostic_segment_ids=diagnostic_segment_ids,
            sim_time=sim_time,
        )
        for index in range(replications)
    ]
    aggregates = aggregate_load_factor_replications(replication_rows)
    sub_seeds = [_replication_seed(seed_text, index) for index in range(replications)]

    return {
        "tanggal": resolved_date,
        "replication_count": replications,
        "master_seed": seed_text,
        "sub_seeds": sub_seeds,
        "replications": replication_rows,
        **aggregates,
    }


def _route_signature(route: dict) -> tuple:
    return tuple(
        (
            edge.get("asal"),
            edge.get("tujuan"),
            edge.get("tipe"),
            edge.get("segmen_id"),
            edge.get("koridor_id"),
        )
        for edge in route.get("path", [])
    )


def _segment_ids_from_signature(signature: tuple | None) -> set[str]:
    if not signature:
        return set()
    return {
        str(edge[3])
        for edge in signature
        if len(edge) >= 5 and edge[2] == "segmen" and edge[3] is not None
    }


def _pairwise_spearman(rank_a: list[str], rank_b: list[str]) -> float | None:
    common = [signature for signature in rank_a if signature in rank_b]
    if len(common) < 2:
        return None
    rank_map_a = {signature: index for index, signature in enumerate(rank_a)}
    rank_map_b = {signature: index for index, signature in enumerate(rank_b)}
    n = len(common)
    diff_sq = sum((rank_map_a[sig] - rank_map_b[sig]) ** 2 for sig in common)
    return float(1 - (6 * diff_sq) / (n * (n**2 - 1)))


def _pairwise_kendall(rank_a: list[str], rank_b: list[str]) -> float | None:
    common = [signature for signature in rank_a if signature in rank_b]
    if len(common) < 2:
        return None
    rank_map_a = {signature: index for index, signature in enumerate(rank_a)}
    rank_map_b = {signature: index for index, signature in enumerate(rank_b)}

    concordant = discordant = 0
    for left, right in combinations(common, 2):
        a_order = rank_map_a[left] - rank_map_a[right]
        b_order = rank_map_b[left] - rank_map_b[right]
        if a_order * b_order > 0:
            concordant += 1
        elif a_order * b_order < 0:
            discordant += 1

    total = concordant + discordant
    if total <= 0:
        return None
    return float((concordant - discordant) / total)


def _active_segment_snapshot_for_replication(
    ctx: SimulationContext,
    segment_loads: dict[str, dict[str, float]],
    sim_time: int,
) -> dict[str, float]:
    values: dict[str, list[float]] = defaultdict(list)
    for instance in ctx.instances:
        if not _is_active(instance, sim_time):
            continue
        segment_id = _current_segment_id(instance, sim_time)
        if segment_id is None:
            continue
        load = segment_loads.get(instance["trip_instance_id"], {}).get(segment_id)
        if load is not None:
            values[segment_id].append(display_load_factor(load))
    return {
        segment_id: display_load_factor(sum(loads) / len(loads))
        for segment_id, loads in values.items()
        if loads
    }


def _daily_mean_snapshot_for_replication(
    ctx: SimulationContext,
    trip_loads: dict[str, dict],
) -> dict[str, float]:
    values_by_koridor: dict[str, list[float]] = defaultdict(list)
    for instance in ctx.instances:
        kid = _normalize_koridor_id(instance.get("koridor_key"))
        if kid not in SCOPED_KORIDOR:
            continue
        trip_id = instance.get("trip_instance_id")
        payload = trip_loads.get(trip_id, {})
        load_factor = payload.get("trip_load_factor")
        if load_factor is None:
            continue
        values_by_koridor[kid].append(float(load_factor))

    snapshot: dict[str, float] = {}
    for kid in SCOPED_KORIDOR:
        values = values_by_koridor.get(kid, [])
        if not values:
            snapshot[kid] = FALLBACK_LOAD_FACTOR
            continue
        snapshot[kid] = display_load_factor(sum(values) / len(values))
    return snapshot


def _realtime_trip_load_snapshot(trip_loads: dict[str, dict]) -> dict[str, float]:
    return {
        trip_id: display_load_factor(float(payload.get("trip_load_factor", FALLBACK_LOAD_FACTOR)))
        for trip_id, payload in trip_loads.items()
    }

def _next_trip_for_segment(
    ctx: SimulationContext,
    segment_id: str,
    boarding_halte: str,
    alighting_halte: str,
    corridor_id: Any,
    boarding_time: int,
) -> tuple[dict, dict, str] | None:
    candidates: list[tuple[int, str, dict, dict, str]] = []
    for instance in ctx.instances:
        stops = instance.get("stops", [])
        segment_ids = instance.get("segment_ids") or []
        for index, stop in enumerate(stops[:-1]):
            next_stop = stops[index + 1]
            if stop.get("halte_id") != boarding_halte:
                continue
            if next_stop.get("halte_id") != alighting_halte:
                continue
            if stop.get("koridor_id") != corridor_id:
                continue
            arrival = int(stop.get("waktu_tiba_detik", 0))
            if arrival < boarding_time:
                continue
            matched_segment_id = segment_id if segment_id in segment_ids else ""
            if not matched_segment_id and index < len(segment_ids):
                candidate_segment_id = str(segment_ids[index])
                candidate_segment = ctx.segmen_by_id.get(candidate_segment_id, {})
                if (
                    str(candidate_segment.get("koridor_id")) == str(corridor_id)
                    and str(candidate_segment.get("halte_asal")) == boarding_halte
                    and str(candidate_segment.get("halte_tujuan")) == alighting_halte
                ):
                    matched_segment_id = candidate_segment_id
            if not matched_segment_id:
                for candidate_id, candidate_segment in ctx.segmen_by_id.items():
                    if (
                        str(candidate_segment.get("koridor_id")) == str(corridor_id)
                        and str(candidate_segment.get("halte_asal")) == boarding_halte
                        and str(candidate_segment.get("halte_tujuan")) == alighting_halte
                        and candidate_id in segment_ids
                    ):
                        matched_segment_id = candidate_id
                        break
            if not matched_segment_id:
                matched_segment_id = segment_id
            if not matched_segment_id:
                continue
            candidates.append((
                arrival,
                str(instance["trip_instance_id"]),
                instance,
                stop,
                matched_segment_id,
            ))
            break
    if not candidates:
        for instance in ctx.instances:
            for stop in instance.get("stops", []):
                if stop.get("halte_id") != boarding_halte:
                    continue
                if stop.get("koridor_id") != corridor_id:
                    continue
                arrival = int(stop.get("waktu_tiba_detik", 0))
                if arrival < boarding_time:
                    continue
                candidates.append((
                    arrival,
                    str(instance["trip_instance_id"]),
                    instance,
                    stop,
                    segment_id,
                ))
                break
    if not candidates:
        for instance in ctx.instances:
            if not instance.get("stops"):
                continue
            if instance["stops"][0].get("koridor_id") != corridor_id:
                continue
            departure = int(instance.get("first_stop_departure_time", 0))
            if departure < boarding_time:
                continue
            candidates.append((
                departure,
                str(instance["trip_instance_id"]),
                instance,
                instance["stops"][0],
                segment_id,
            ))
    if not candidates:
        return None
    _, _, instance, stop, matched_segment_id = min(
        candidates,
        key=lambda row: (row[0], row[1]),
    )
    return instance, stop, matched_segment_id


def _boarding_trip_route_weights(
    ctx: SimulationContext,
    route: dict,
    replication: dict,
    sim_time: int,
    daily_mean_by_koridor: dict,
) -> list[dict]:
    """Resolve one sampled trip per route segment at its accumulated boarding time."""
    segment_loads = replication.get("segment_loads", {})
    result: list[dict] = []
    current_time = sim_time

    for edge in route.get("path", []):
        current_time += int(edge.get("waktu_tempuh_detik", 0) or 0)
        if edge.get("tipe") != "segmen":
            continue

        segment_id = str(edge.get("segmen_id"))
        corridor_id = edge.get("koridor_id")
        boarding_time = current_time - int(edge.get("waktu_tempuh_detik", 0) or 0)
        selected = _next_trip_for_segment(
            ctx,
            segment_id,
            str(edge.get("asal")),
            str(edge.get("tujuan")),
            corridor_id,
            boarding_time,
        )

        instance = selected[0] if selected is not None else None
        sampled_segment_id = selected[2] if selected is not None else segment_id
        sampled_load = (
            segment_loads.get(instance["trip_instance_id"], {}).get(sampled_segment_id)
            if instance is not None
            else None
        )
        if sampled_load is None and instance is not None:
            trip_payload = replication.get("trip_loads", {}).get(
                instance["trip_instance_id"],
                {},
            )
            segment_seed = _stable_int_seed(
                replication["replication_seed"],
                str(corridor_id),
                instance["trip_instance_id"],
                segment_id,
                "segment",
            )
            _, scaled_segment_draw = _scaled_poisson_draw(
                segment_seed,
                MC_SEGMENT_POISSON_LAMBDA,
            )
            sampled_load = float(
                trip_payload.get("trip_load_factor", FALLBACK_LOAD_FACTOR)
            ) * scaled_segment_draw
        if sampled_load is not None:
            value = display_load_factor(float(sampled_load))
            source = "boarding_trip_segment"
            trip_instance_id = instance["trip_instance_id"]
        else:
            value = float(
                daily_mean_by_koridor.get(
                    corridor_id,
                    daily_mean_by_koridor.get(str(corridor_id), FALLBACK_LOAD_FACTOR),
                )
            )
            source = "corridor_daily_mean"
            trip_instance_id = None

        result.append({
            "segment_id": segment_id,
            "koridor_id": corridor_id,
            "boarding_halte": edge.get("asal"),
            "boarding_time": boarding_time,
            "trip_instance_id": trip_instance_id,
            "value_used": value,
            "source": source,
        })
    return result


def build_recommendation_crowding_snapshot(
    ctx: SimulationContext,
    tanggal: str | None = None,
    sim_time: int | None = None,
    request_seed_parts: tuple[Any, ...] = (),
) -> dict:
    """Build a single seeded Monte Carlo crowding snapshot for route recommendation."""
    seed_parts = ("rute_rekomendasi", tanggal, sim_time, *request_seed_parts)
    master_seed = build_request_master_seed(*seed_parts)
    replication = simulate_load_factor_replication(ctx, tanggal, 0, master_seed)
    trip_loads = replication["trip_loads"]
    return {
        "master_seed": master_seed,
        "replication": replication,
        "segment_crowding": _active_segment_snapshot_for_replication(
            ctx,
            replication.get("segment_loads", {}),
            int(sim_time or 0),
        ),
        "daily_mean_by_koridor": _daily_mean_snapshot_for_replication(ctx, trip_loads),
        "realtime_kepadatan": _realtime_trip_load_snapshot(trip_loads),
    }


def _route_pipeline_for_replication(
    ctx: SimulationContext,
    graph_data: dict[str, Any],
    replication: dict,
    scenario: dict,
    jam: int,
    hari_tipe: str,
    sim_time: int,
    tanggal: str | None,
) -> dict:
    daily_mean_by_koridor = {
        kid: daily_mean_for(
            ctx,
            tanggal,
            kid,
            simulation_run_id=str(replication["replication_seed"]),
        )
        for kid in SCOPED_KORIDOR
    }
    graph = build_graph(
        graph_data,
        jam=jam,
        hari_tipe=hari_tipe,
            segment_crowding=None,
        daily_mean_by_koridor=daily_mean_by_koridor,
    )
    routes = dijkstra(
        graph,
        scenario["halte_asal"],
        scenario["halte_tujuan"],
        k=KANDIDAT_RUTE_DEFAULT,
    )
    baseline_routes = dijkstra(
        graph,
        scenario["halte_asal"],
        scenario["halte_tujuan"],
        k=1,
    )
    raw_baseline_route = baseline_routes[0] if baseline_routes else None
    baseline_weights = (
        _boarding_trip_route_weights(
            ctx,
            raw_baseline_route,
            replication,
            sim_time,
            daily_mean_by_koridor,
        )
        if raw_baseline_route is not None
        else []
    )
    baseline_density = (
        sum(row["value_used"] for row in baseline_weights) / len(baseline_weights)
        if baseline_weights
        else None
    )
    realtime_kepadatan = _realtime_trip_load_snapshot(replication.get("trip_loads", {}))
    ranked_routes: list[dict] = []
    for route in routes:
        boarding_weights = _boarding_trip_route_weights(
            ctx,
            route,
            replication,
            sim_time,
            daily_mean_by_koridor,
        )
        weight_by_segment = {
            row["segment_id"]: row for row in boarding_weights
        }
        route = {
            **route,
            "path": [
                {
                    **edge,
                    "bobot_kepadatan": weight_by_segment.get(
                        str(edge.get("segmen_id")), {}
                    ).get("value_used", edge.get("bobot_kepadatan", FALLBACK_LOAD_FACTOR)),
                }
                for edge in route.get("path", [])
            ],
        }
        formatted = format_rute(route, graph_data)
        select_bus_per_segmen(formatted["segmen"], sim_time, ctx.jadwal, realtime_kepadatan)
        density_values = [
            float(item.get("kepadatan", 0.0))
            for item in formatted["segmen"]
            if item.get("tipe") == "naik"
        ]
        if density_values:
            rata_kepadatan = sum(density_values) / len(density_values)
        else:
            rata_kepadatan = float(formatted.get("rata_kepadatan", 0.0))
        formatted["rata_kepadatan"] = round(rata_kepadatan, 3)
        formatted["density_norm"] = round(min(rata_kepadatan, 1.0), 3)
        formatted["skor"] = round(float(formatted.get("density_norm", 0.0)), 4)
        ranked_routes.append({
            "raw": route,
            "formatted": formatted,
            "boarding_weights": boarding_weights,
            "route_density": (
                sum(row["value_used"] for row in boarding_weights)
                / len(boarding_weights)
                if boarding_weights
                else float(formatted.get("rata_kepadatan", 0.0))
            ),
        })

    ranked_routes.sort(key=lambda item: (
        float(item["formatted"].get("density_norm", 0.0)),
        float(item["formatted"].get("primary_score", 0.0)),
    ))

    top_route_segment_weights: list[dict] = []
    if ranked_routes:
        top_route_segment_weights = ranked_routes[0]["boarding_weights"]

    candidate_details = [
        {
            "candidate_index": index,
            "route_signature": _route_signature(item["raw"]),
            "primary_score": float(item["formatted"].get("primary_score", 0.0)),
            "density_norm": float(item["formatted"].get("density_norm", 0.0)),
            "route_density": float(item["route_density"]),
            "is_baseline_candidate": (
                raw_baseline_route is not None
                and _route_signature(item["raw"]) == _route_signature(raw_baseline_route)
            ),
            "is_recommended_candidate": index == 1,
        }
        for index, item in enumerate(ranked_routes, start=1)
    ]

    return {
        "ranked_routes": ranked_routes,
        "top_route_signature": _route_signature(ranked_routes[0]["raw"]) if ranked_routes else None,
        "top_route": ranked_routes[0]["formatted"] if ranked_routes else None,
        "top_route_segment_weights": top_route_segment_weights,
        "candidate_details": candidate_details,
        "baseline_route_signature": (
            _route_signature(raw_baseline_route)
            if raw_baseline_route is not None
            else None
        ),
        "baseline_primary_score": None,
        "baseline_density": baseline_density,
        "recommended_density": (
            float(ranked_routes[0]["route_density"])
            if ranked_routes
            else None
        ),
        "baseline_route_metrics": (
            {
                "total_waktu_detik": raw_baseline_route.get("total_waktu_detik", 0),
                "total_jarak_meter": raw_baseline_route.get("total_jarak_meter", 0.0),
                "transit_count": raw_baseline_route.get("transit_count", 0),
            }
            if raw_baseline_route is not None
            else None
        ),
        "baseline_segment_weights": baseline_weights,
    }


def run_routing_sensitivity(
    ctx: SimulationContext,
    graph_data: dict[str, Any],
    scenarios: list[dict],
    load_factor_experiment: dict,
    jam: int,
    hari_tipe: str,
    sim_time: int,
    tanggal: str | None = None,
) -> dict:
    replications = load_factor_experiment.get("replications", [])
    scenario_rows: list[dict] = []

    for scenario in scenarios:
        replication_rows: list[dict] = []
        ranking_signatures: list[list[str]] = []
        top_signatures: list[tuple | None] = []
        route_segment_series: dict[str, list[float]] = defaultdict(list)

        for replication in replications:
            route_result = _route_pipeline_for_replication(
                ctx,
                graph_data,
                replication,
                scenario,
                jam,
                hari_tipe,
                sim_time,
                tanggal,
            )
            ranked_routes = route_result["ranked_routes"]
            candidate_signatures = [_route_signature(item["raw"]) for item in ranked_routes]
            ranking_signatures.append(candidate_signatures)
            top_route_signature = route_result["top_route_signature"]
            top_signatures.append(top_route_signature)
            top_route_segment_ids = _segment_ids_from_signature(top_route_signature)
            top_route_segment_weights = route_result["top_route_segment_weights"]
            active_segment_loads = {
                row["segment_id"]: row["value_used"]
                for row in top_route_segment_weights
                if row["source"] == "boarding_trip_segment"
            }
            for row in top_route_segment_weights:
                route_segment_series[row["segment_id"]].append(row["value_used"])
            replication_rows.append({
                "replication_index": replication["replication_index"],
                "replication_seed": replication["replication_seed"],
                "top_route_signature": top_route_signature,
                "top_route": route_result["top_route"],
                "candidate_rankings": candidate_signatures,
                "active_segment_loads": active_segment_loads,
                "top_route_segment_weights": route_result["top_route_segment_weights"],
                "baseline_route_signature": route_result["baseline_route_signature"],
                "baseline_primary_score": route_result["baseline_primary_score"],
                "baseline_density": route_result["baseline_density"],
                "recommended_density": route_result["recommended_density"],
                "candidate_details": route_result["candidate_details"],
            })

        top_counts = Counter(signature for signature in top_signatures if signature is not None)
        baseline_signatures = [
            row["baseline_route_signature"]
            for row in replication_rows
            if row["baseline_route_signature"] is not None
        ]
        baseline_counts = Counter(baseline_signatures)
        mode_signature = None
        mode_count = 0
        if top_counts:
            mode_signature, mode_count = top_counts.most_common(1)[0]
        baseline_mode_signature = (
            baseline_counts.most_common(1)[0][0]
            if baseline_counts
            else None
        )
        stability_rate = (mode_count / len(top_signatures)) if top_signatures else 0.0

        spearman_values: list[float] = []
        kendall_values: list[float] = []
        for left_index, right_index in combinations(range(len(ranking_signatures)), 2):
            spearman = _pairwise_spearman(
                ranking_signatures[left_index],
                ranking_signatures[right_index],
            )
            kendall = _pairwise_kendall(
                ranking_signatures[left_index],
                ranking_signatures[right_index],
            )
            if spearman is not None:
                spearman_values.append(spearman)
            if kendall is not None:
                kendall_values.append(kendall)

        scenario_rows.append({
            "name": scenario.get("name") or f"{scenario['halte_asal']}->{scenario['halte_tujuan']}",
            "halte_asal": scenario["halte_asal"],
            "halte_tujuan": scenario["halte_tujuan"],
            "replications": replication_rows,
            "top_route_stability_rate": round(stability_rate, 4),
            "top_route_mode_signature": mode_signature,
            "top_route_mode_count": mode_count,
            "unique_top_route_count": len(top_counts),
            "segment_aggregates": {
                segment_id: _series_stats(values)
                for segment_id, values in sorted(route_segment_series.items())
            },
            "baseline_route_signature": (
                baseline_mode_signature
            ),
            "recommended_route_signature": (
                mode_signature
            ),
            "baseline_density_mean": (
                mean(row["baseline_density"] for row in replication_rows)
                if replication_rows
                else None
            ),
            "recommended_density_mean": (
                mean(row["recommended_density"] for row in replication_rows)
                if replication_rows
                else None
            ),
            "average_spearman_rho": round(mean(spearman_values), 4) if spearman_values else None,
            "average_kendall_tau": round(mean(kendall_values), 4) if kendall_values else None,
        })

    return {
        "scenario_count": len(scenario_rows),
        "scenarios": scenario_rows,
    }


def run_monte_carlo_experiment(
    ctx: SimulationContext,
    graph_data: dict[str, Any],
    scenarios: list[dict],
    replications: int = MC_DEFAULT_REPLICATIONS,
    master_seed: int | str | None = None,
    tanggal: str | None = None,
    jam: int = 0,
    hari_tipe: str = "weekday",
    sim_time: int = 0,
    diagnostic_segment_ids: set[str] | None = None,
) -> dict:
    load_factor_experiment = run_load_factor_monte_carlo(
        ctx,
        tanggal=tanggal,
        replications=replications,
        master_seed=master_seed,
        diagnostic_segment_ids=diagnostic_segment_ids,
        sim_time=sim_time,
    )
    routing_sensitivity = (
        run_routing_sensitivity(
            ctx,
            graph_data,
            scenarios,
            load_factor_experiment,
            jam=jam,
            hari_tipe=hari_tipe,
            sim_time=sim_time,
            tanggal=tanggal,
        )
        if scenarios
        else None
    )

    return {
        "load_factor": load_factor_experiment,
        "routing_sensitivity": routing_sensitivity,
    }