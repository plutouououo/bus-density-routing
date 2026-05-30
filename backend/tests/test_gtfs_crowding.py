import math

from services.gtfs_simulation import BUS_CAPACITY, SimulationContext, _trip_loads_for_corridor


def _ctx_with_instances(instances: list[dict], ridership: float = 2400.0) -> SimulationContext:
    return SimulationContext(
        instances=instances,
        jadwal={},
        trip_supply_per_koridor={"1": len(instances)},
        daily_mean_load_factor={},
        latest_date="2026-05-01",
        latest_date_per_koridor={"1": "2026-05-01"},
        ridership_by_date_koridor={("2026-05-01", "1"): ridership},
        segmen_by_id={},
        fallback_jadwal={},
    )


def _instance(tid: str, departure_time: int) -> dict:
    return {
        "trip_instance_id": tid,
        "bus_id": tid,
        "trip_id": tid,
        "koridor_id": 1,
        "koridor_key": "1",
        "direction_id": "0",
        "departure_time": departure_time,
        "first_stop_departure_time": departure_time,
        "last_stop_arrival_time": departure_time + 1800,
        "stops": [],
        "segment_ids": [],
    }


def test_time_band_weighted_crowding_preserves_daily_ridership_total():
    instances = [
        _instance("early-1", 5 * 3600),
        _instance("morning-1", 6 * 3600),
        _instance("midday-1", 9 * 3600),
        _instance("evening-1", 16 * 3600),
        _instance("night-1", 20 * 3600),
        _instance("late-1", 22 * 3600),
    ]
    ctx = _ctx_with_instances(instances, ridership=1234.5)

    loads, debug = _trip_loads_for_corridor(ctx, "2026-05-01", "1", "run-a")

    total_passengers = sum(row["estimated_passengers"] for row in loads.values())
    assert math.isclose(total_passengers, 1234.5, rel_tol=0, abs_tol=1e-9)
    assert math.isclose(debug["total_allocated_passengers"], 1234.5, rel_tol=0, abs_tol=1e-9)
    assert math.isclose(debug["allocation_error"], 0.0, rel_tol=0, abs_tol=1e-9)
    assert all(row["trip_load_factor"] == row["estimated_passengers"] / BUS_CAPACITY for row in loads.values())


def test_peak_trips_get_higher_baseline_than_night_trips():
    instances = []
    for index in range(10):
        instances.append(_instance(f"morning-{index}", 6 * 3600 + index * 60))
        instances.append(_instance(f"night-{index}", 20 * 3600 + index * 60))
    ctx = _ctx_with_instances(instances, ridership=1600.0)

    loads, _debug = _trip_loads_for_corridor(ctx, "2026-05-01", "1", "run-a")

    morning_loads = [
        row["trip_load_factor"] for row in loads.values() if row["time_band"] == "morning_peak"
    ]
    night_loads = [row["trip_load_factor"] for row in loads.values() if row["time_band"] == "night"]
    assert min(morning_loads) > max(night_loads)


def test_same_date_and_run_id_are_reproducible():
    instances = [
        _instance("morning-1", 6 * 3600),
        _instance("morning-2", 7 * 3600),
        _instance("evening-1", 16 * 3600),
    ]
    ctx = _ctx_with_instances(instances, ridership=900.0)

    first, _debug_first = _trip_loads_for_corridor(ctx, "2026-05-01", "1", "same-run")
    second, _debug_second = _trip_loads_for_corridor(ctx, "2026-05-01", "1", "same-run")

    assert first == second
