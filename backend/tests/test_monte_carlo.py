import math
from types import SimpleNamespace

from routers import rute as rute_router
from services.gtfs_simulation import SimulationContext
from services.monte_carlo import (
    MC_POISSON_SCALE_FACTOR,
    _scaled_poisson_draw,
    run_load_factor_monte_carlo,
    run_routing_sensitivity,
)


def _instance(tid: str, koridor_id: int, stops: list[dict], segment_ids: list[str]) -> dict:
    departure_time = stops[0]["waktu_berangkat_detik"]
    return {
        "trip_instance_id": tid,
        "bus_id": tid,
        "trip_id": tid,
        "koridor_id": koridor_id,
        "koridor_key": str(koridor_id),
        "direction_id": "0",
        "departure_time": departure_time,
        "first_stop_departure_time": departure_time,
        "last_stop_arrival_time": stops[-1]["waktu_tiba_detik"],
        "stops": stops,
        "segment_ids": segment_ids,
    }


def _stop(halte_id: str, koridor_id: int, tiba: int, berangkat: int | None = None) -> dict:
    return {
        "halte_id": halte_id,
        "koridor_id": koridor_id,
        "waktu_tiba_detik": tiba,
        "waktu_berangkat_detik": berangkat if berangkat is not None else tiba,
    }


def _mc_context() -> SimulationContext:
    route_1_stops = [
        _stop("A", 1, 8 * 3600, 8 * 3600),
        _stop("B", 1, 8 * 3600 + 300),
        _stop("C", 1, 8 * 3600 + 600),
    ]
    route_2_stops = [
        _stop("A", 2, 8 * 3600, 8 * 3600),
        _stop("D", 2, 8 * 3600 + 300),
        _stop("C", 2, 8 * 3600 + 600),
    ]
    instances = [
        _instance("T1", 1, route_1_stops, ["S1", "S2"]),
        _instance("T2", 2, route_2_stops, ["S3", "S4"]),
    ]
    return SimulationContext(
        instances=instances,
        jadwal={
            "T1": route_1_stops,
            "T2": route_2_stops,
        },
        trip_supply_per_koridor={"1": 1, "2": 1},
        daily_mean_load_factor={("2026-05-01", "1"): 0.5, ("2026-05-01", "2"): 0.5},
        latest_date="2026-05-01",
        latest_date_per_koridor={"1": "2026-05-01", "2": "2026-05-01"},
        recent_dates_per_koridor={"1": ["2026-05-01"], "2": ["2026-05-01"]},
        ridership_by_date_koridor={("2026-05-01", "1"): 1000.0, ("2026-05-01", "2"): 1000.0},
        segmen_by_id={
            "S1": {"segmen_id": "S1"},
            "S2": {"segmen_id": "S2"},
            "S3": {"segmen_id": "S3"},
            "S4": {"segmen_id": "S4"},
        },
        fallback_jadwal={},
    )


def _graph_data() -> dict:
    return {
        "segmen": [
            {"segmen_id": "S1", "koridor_id": 1, "halte_asal": "A", "halte_tujuan": "B", "waktu_tempuh_detik": 300},
            {"segmen_id": "S2", "koridor_id": 1, "halte_asal": "B", "halte_tujuan": "C", "waktu_tempuh_detik": 300},
            {"segmen_id": "S3", "koridor_id": 2, "halte_asal": "A", "halte_tujuan": "D", "waktu_tempuh_detik": 300},
            {"segmen_id": "S4", "koridor_id": 2, "halte_asal": "D", "halte_tujuan": "C", "waktu_tempuh_detik": 300},
        ],
        "kepadatan_bus": [],
        "halte": {
            "A": {"halte_id": "A", "nama": "A", "lat": 0.0, "lng": 0.0},
            "B": {"halte_id": "B", "nama": "B", "lat": 0.0, "lng": 0.001},
            "C": {"halte_id": "C", "nama": "C", "lat": 0.0, "lng": 0.002},
            "D": {"halte_id": "D", "nama": "D", "lat": 0.001, "lng": 0.001},
        },
        "koridor_halte": [],
        "koridor": {
            1: {"koridor_id": 1, "nama_pendek": "K1", "nama_panjang": "Koridor 1"},
            2: {"koridor_id": 2, "nama_pendek": "K2", "nama_panjang": "Koridor 2"},
        },
        "halte_to_koridor": {"A": {1, 2}, "B": {1}, "C": {1, 2}, "D": {2}},
    }


def test_load_factor_monte_carlo_is_reproducible_and_balances_totals():
    ctx = _mc_context()

    first = run_load_factor_monte_carlo(ctx, tanggal="2026-05-01", replications=6, master_seed=1234)
    second = run_load_factor_monte_carlo(ctx, tanggal="2026-05-01", replications=6, master_seed=1234)

    assert first["master_seed"] == second["master_seed"]
    assert first["sub_seeds"] == second["sub_seeds"]
    assert first["replications"] == second["replications"]

    for replication in first["replications"]:
        assert math.isclose(replication["allocated_passenger_total"], 2000.0, rel_tol=0, abs_tol=1e-9)
        assert math.isclose(replication["trip_load_factor_total"], sum(
            payload["trip_load_factor"] for payload in replication["trip_loads"].values()
        ), rel_tol=0, abs_tol=1e-12)

    assert set(first["trip_aggregates"]) == {"T1", "T2"}
    assert set(first["segment_aggregates"]) == {"S1", "S2", "S3", "S4"}
    assert "mean" in first["trip_aggregates"]["T1"]
    assert "std_dev" in first["segment_aggregates"]["S1"]
    assert "p05" in first["segment_aggregates"]["S1"]
    assert "p95" in first["segment_aggregates"]["S1"]


def test_poisson_draw_uses_configured_rate_scaling():
    raw_draw, scaled_draw = _scaled_poisson_draw(1234, 1.0)

    assert MC_POISSON_SCALE_FACTOR == 25
    assert raw_draw >= 0
    assert scaled_draw == raw_draw / MC_POISSON_SCALE_FACTOR


def test_routing_sensitivity_reports_stability_and_rank_correlation():
    ctx = _mc_context()
    graph_data = _graph_data()
    load_factor_experiment = {
        "replications": [
            {
                "replication_index": 0,
                "replication_seed": 111,
                "trip_loads": {
                    "T1": {"trip_load_factor": 0.20, "estimated_passengers": 16.0, "tanggal": "2026-05-01"},
                    "T2": {"trip_load_factor": 0.90, "estimated_passengers": 72.0, "tanggal": "2026-05-01"},
                },
                "segment_loads": {
                    "T1": {"S1": 0.20, "S2": 0.20},
                    "T2": {"S3": 0.90, "S4": 0.90},
                },
            },
            {
                "replication_index": 1,
                "replication_seed": 222,
                "trip_loads": {
                    "T1": {"trip_load_factor": 0.85, "estimated_passengers": 68.0, "tanggal": "2026-05-01"},
                    "T2": {"trip_load_factor": 0.25, "estimated_passengers": 20.0, "tanggal": "2026-05-01"},
                },
                "segment_loads": {
                    "T1": {"S1": 0.85, "S2": 0.85},
                    "T2": {"S3": 0.25, "S4": 0.25},
                },
            },
        ]
    }

    result = run_routing_sensitivity(
        ctx,
        graph_data,
        scenarios=[{"name": "same-corridor", "halte_asal": "A", "halte_tujuan": "C"}],
        load_factor_experiment=load_factor_experiment,
        jam=8,
        hari_tipe="weekday",
        sim_time=8 * 3600,
        tanggal="2026-05-01",
    )

    scenario = result["scenarios"][0]
    assert scenario["top_route_stability_rate"] == 0.5
    assert scenario["unique_top_route_count"] == 2
    assert scenario["top_route_mode_count"] == 1
    assert scenario["average_spearman_rho"] is not None
    assert scenario["average_kendall_tau"] is not None
    assert len(scenario["replications"]) == 2
    assert scenario["replications"][0]["top_route_signature"] != scenario["replications"][1]["top_route_signature"]
    assert scenario["segment_aggregates"]
    assert all(
        stats["count"] >= 1
        for stats in scenario["segment_aggregates"].values()
    )
    assert all(
        "active_segment_loads" in replication
        for replication in scenario["replications"]
    )
    assert all(
        replication["top_route_segment_weights"]
        for replication in scenario["replications"]
    )
    assert all(
        weight["source"] in {
            "boarding_trip_segment",
            "active_segment",
            "corridor_daily_mean",
            "kepadatan_bus_or_default",
        }
        for replication in scenario["replications"]
        for weight in replication["top_route_segment_weights"]
    )


def test_rekomendasi_uses_seeded_monte_carlo_snapshot(monkeypatch):
    ctx = _mc_context()
    graph_data = _graph_data()
    captured = {}

    def fake_snapshot(simulation_context, tanggal=None, sim_time=None, request_seed_parts=()):
        captured["simulation_context"] = simulation_context
        captured["tanggal"] = tanggal
        captured["sim_time"] = sim_time
        captured["request_seed_parts"] = request_seed_parts
        return {
            "master_seed": "seed-123",
            "replication": {},
            "segment_crowding": {"S1": 0.25, "S2": 0.25, "S3": 0.75, "S4": 0.75},
            "daily_mean_by_koridor": {"1": 0.25, "2": 0.75},
            "realtime_kepadatan": {"T1": 0.25, "T2": 0.75},
        }

    monkeypatch.setattr(rute_router, "build_recommendation_crowding_snapshot", fake_snapshot)
    monkeypatch.setattr(rute_router, "select_bus_per_segmen", lambda *args, **kwargs: None)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                graph_data=graph_data,
                jadwal=ctx.jadwal,
                simulation_context=ctx,
            )
        )
    )

    result = rute_router.rekomendasi(
        rute_router.RuteRequest(
            halte_asal="A",
            halte_tujuan="C",
            jam=8,
            hari_tipe="weekday",
            sim_time=8 * 3600,
            tanggal="2026-05-01",
            simulation_run_id="run-1",
        ),
        request,
    )

    assert captured["request_seed_parts"] == ("A", "C", 8, "weekday", 8 * 3600, "run-1")
    assert captured["tanggal"] == "2026-05-01"
    assert captured["sim_time"] == 8 * 3600
    assert isinstance(result, list)
    assert result