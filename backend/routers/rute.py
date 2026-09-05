"""Router rekomendasi rute TransJakarta untuk kios halte."""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.bus_selector import MAX_ETA_DETIK_DEFAULT, select_bus_per_segmen
from services.dijkstra import (
    KANDIDAT_RUTE_DEFAULT,
    MAKS_TRANSIT_DEFAULT,
    build_graph,
    dijkstra,
    format_rute,
    get_realtime_kepadatan,
)
from services.monte_carlo import build_recommendation_crowding_snapshot, run_monte_carlo_experiment
from services.gtfs_simulation import upcoming_buses_for_halte
from services.geo import distance_meters
from services.supabase_client import get_client

router = APIRouter(prefix="/api/rute")

WIB = ZoneInfo("Asia/Jakarta")
HALTE_ALIAS_RADIUS_METER = 80.0


def _jam_sekarang_wib() -> int:
    return datetime.now(WIB).hour


def _hari_tipe_sekarang() -> str:
    # Senin-Jumat (0-4) = weekday; Sabtu-Minggu (5-6) = weekend
    return "weekday" if datetime.now(WIB).weekday() < 5 else "weekend"


def _apply_selected_bus_density(formatted: dict) -> dict:
    """Hitung ulang kepadatan rute setelah bus_rekomendasi dipilih.

    Ini menyelaraskan implementasi dengan Bab 4.6.2: kepadatan rute memakai
    load factor trip/bus yang benar-benar direkomendasikan pada tiap segmen
    naik. Jika bus tidak tersedia, fallback ke kepadatan edge yang sudah ada.
    """
    naik_items = [s for s in formatted["segmen"] if s.get("tipe") == "naik"]
    density_values: list[float] = []
    selected_count = 0

    for item in naik_items:
        rek = item.get("bus_rekomendasi")
        if rek is not None and rek.get("kepadatan") is not None:
            density = float(rek["kepadatan"])
            selected_count += 1
            item["kepadatan"] = round(density, 3)
        else:
            density = float(item.get("kepadatan", 0.0))
        density_values.append(density)

    if not density_values:
        return formatted

    rata_kepadatan = sum(density_values) / len(density_values)

    formatted["rata_kepadatan"] = round(rata_kepadatan, 3)
    density_norm = min(rata_kepadatan, 1.0)
    formatted["density_norm"] = round(density_norm, 3)
    formatted["skor"] = round(density_norm, 4)
    formatted["ranking_phase_2"] = {
        "rata_kepadatan": round(rata_kepadatan, 3),
        "density_norm": round(density_norm, 3),
    }
    formatted["density_source"] = (
        "selected_bus"
        if selected_count == len(density_values)
        else "mixed_edge_fallback"
    )
    return formatted


def _normalized_halte_name(value: str | None) -> str:
    text = " ".join(str(value or "").lower().split())
    for suffix in (" arah utara", " arah selatan", " arah barat", " arah timur"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _halte_aliases(halte_id: str, halte_master: dict) -> list[str]:
    """Cari platform lain dengan nama sama dan jarak dekat.

    Data TransJakarta sering punya dua halte_id untuk dua arah/platform pada
    nama halte yang sama. Untuk input pengguna, keduanya lebih masuk akal
    diperlakukan sebagai satu titik naik/turun.
    """
    halte = halte_master[halte_id]
    nama = _normalized_halte_name(halte.get("nama"))
    aliases: list[tuple[float, str]] = []
    for other_id, other in halte_master.items():
        if _normalized_halte_name(other.get("nama")) != nama:
            continue
        try:
            jarak = distance_meters(halte["lat"], halte["lng"], other["lat"], other["lng"])
        except (TypeError, ValueError, KeyError):
            continue
        if jarak <= HALTE_ALIAS_RADIUS_METER:
            aliases.append((jarak, other_id))
    aliases.sort(key=lambda item: (item[0], item[1] != halte_id, item[1]))
    return [other_id for _, other_id in aliases]


def _dijkstra_with_halte_aliases(
    graph: dict[str, list[dict]],
    halte_master: dict,
    halte_asal: str,
    halte_tujuan: str,
) -> tuple[list[dict], str, str]:
    asal_aliases = _halte_aliases(halte_asal, halte_master)
    tujuan_aliases = _halte_aliases(halte_tujuan, halte_master)

    best_routes: list[dict] = []
    best_pair = (halte_asal, halte_tujuan)
    for asal in asal_aliases:
        for tujuan in tujuan_aliases:
            if asal == tujuan:
                continue
            routes = dijkstra(graph, asal, tujuan, k=KANDIDAT_RUTE_DEFAULT)
            if not routes:
                continue
            candidate_key = (
                routes[0].get("primary_score", 0.0),
                routes[0].get("total_waktu_detik", 0),
                routes[0].get("total_jarak_meter", 0.0),
            )
            if not best_routes:
                best_routes = routes
                best_pair = (asal, tujuan)
                best_key = candidate_key
                continue
            if candidate_key < best_key:
                best_routes = routes
                best_pair = (asal, tujuan)
                best_key = candidate_key

    return best_routes, best_pair[0], best_pair[1]


# ----------------------------------------------------------------------
# POST /api/rute/rekomendasi
# ----------------------------------------------------------------------

class RuteRequest(BaseModel):
    halte_asal: str
    halte_tujuan: str
    jam: int | None = Field(default=None, ge=0, le=23)
    hari_tipe: Literal["weekday", "weekend"] | None = None
    # sim_time (detik dalam hari) dipakai oleh bus_selector untuk hitung ETA
    # bus dan filter bus yang sudah lewat. Optional; fallback ke jam*3600.
    sim_time: int | None = Field(default=None, ge=0)
    tanggal: str | None = None
    simulation_run_id: str | None = None


class RoutingScenario(BaseModel):
    name: str | None = None
    halte_asal: str
    halte_tujuan: str


class MonteCarloRequest(BaseModel):
    jam: int | None = Field(default=None, ge=0, le=23)
    hari_tipe: Literal["weekday", "weekend"] | None = None
    sim_time: int | None = Field(default=None, ge=0)
    tanggal: str | None = None
    simulation_run_id: str | None = None
    master_seed: int | str | None = None
    replications: int = Field(default=100, ge=1)
    routing_scenarios: list[RoutingScenario] = Field(default_factory=list)
    diagnostic_segment_ids: list[str] = Field(default_factory=list)


@router.post("/monte-carlo")
def monte_carlo(req: MonteCarloRequest, request: Request) -> dict:
    simulation_context = getattr(request.app.state, "simulation_context", None)
    if simulation_context is None or not simulation_context.instances:
        raise HTTPException(503, "simulation_context tidak tersedia")

    jam = req.jam if req.jam is not None else _jam_sekarang_wib()
    hari_tipe = req.hari_tipe or _hari_tipe_sekarang()
    sim_time = req.sim_time if req.sim_time is not None else jam * 3600

    scenarios = [scenario.model_dump() for scenario in req.routing_scenarios]
    result = run_monte_carlo_experiment(
        simulation_context,
        request.app.state.graph_data,
        scenarios,
        replications=req.replications,
        master_seed=req.master_seed,
        tanggal=req.tanggal,
        jam=jam,
        hari_tipe=hari_tipe,
        sim_time=sim_time,
        diagnostic_segment_ids=set(req.diagnostic_segment_ids),
    )
    result["request"] = {
        "jam": jam,
        "hari_tipe": hari_tipe,
        "sim_time": sim_time,
        "tanggal": req.tanggal,
        "simulation_run_id": req.simulation_run_id,
        "master_seed": req.master_seed,
        "replications": req.replications,
    }
    return result


@router.post("/rekomendasi")
def rekomendasi(req: RuteRequest, request: Request) -> list[dict]:
    graph_data = request.app.state.graph_data
    jadwal: dict = request.app.state.jadwal
    simulation_context = getattr(request.app.state, "simulation_context", None)
    halte_master: dict = graph_data["halte"]

    if req.halte_asal not in halte_master:
        raise HTTPException(
            404, f"halte_asal '{req.halte_asal}' tidak ditemukan"
        )
    if req.halte_tujuan not in halte_master:
        raise HTTPException(
            404, f"halte_tujuan '{req.halte_tujuan}' tidak ditemukan"
        )
    if req.halte_asal == req.halte_tujuan:
        raise HTTPException(
            400, "halte_asal dan halte_tujuan tidak boleh sama"
        )

    jam = req.jam if req.jam is not None else _jam_sekarang_wib()
    hari_tipe = req.hari_tipe or _hari_tipe_sekarang()
    sim_time = req.sim_time if req.sim_time is not None else jam * 3600

    segment_crowding = None
    daily_mean_by_koridor = None
    realtime_kepadatan = None
    if simulation_context is not None and simulation_context.instances:
        crowding_snapshot = build_recommendation_crowding_snapshot(
            simulation_context,
            tanggal=req.tanggal,
            sim_time=sim_time,
            request_seed_parts=(
                req.halte_asal,
                req.halte_tujuan,
                jam,
                hari_tipe,
                sim_time,
                req.simulation_run_id,
            ),
        )
        segment_crowding = crowding_snapshot["segment_crowding"]
        daily_mean_by_koridor = crowding_snapshot["daily_mean_by_koridor"]
        realtime_kepadatan = crowding_snapshot["realtime_kepadatan"]

    graph = build_graph(
        graph_data,
        jam=jam,
        hari_tipe=hari_tipe,
        segment_crowding=segment_crowding,
        daily_mean_by_koridor=daily_mean_by_koridor,
    )

    try:
        rute_list, resolved_asal, resolved_tujuan = _dijkstra_with_halte_aliases(
            graph, halte_master, req.halte_asal, req.halte_tujuan
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not rute_list:
        raise HTTPException(
            404,
            f"Tidak ditemukan rute dari '{halte_master[req.halte_asal]['nama']}' "
            f"ke '{halte_master[req.halte_tujuan]['nama']}' "
            f"dalam batas {MAKS_TRANSIT_DEFAULT + 1} koridor.",
        )

    if resolved_asal != req.halte_asal or resolved_tujuan != req.halte_tujuan:
        print(
            "[rute_rekomendasi] halte_alias_resolved "
            f"asal_request={req.halte_asal} asal_graph={resolved_asal} "
            f"tujuan_request={req.halte_tujuan} tujuan_graph={resolved_tujuan}"
        )

    if realtime_kepadatan is None:
        realtime_kepadatan = get_realtime_kepadatan(graph_data, jam=jam, hari_tipe=hari_tipe)

    debug_items_pre = []
    for idx, r in enumerate(rute_list, start=1):
        debug_items_pre.append(
            "pre_rank={rank} primary={primary:.4f} waktu={waktu}m jarak={jarak:.1f}m "
            "transfer={transfer} edge_kepadatan={kepadatan:.3f}".format(
                rank=idx,
                primary=float(r.get("primary_score", 0.0)),
                waktu=round(float(r.get("total_waktu_detik", 0.0)) / 60),
                jarak=float(r.get("total_jarak_meter", 0.0)),
                kepadatan=float(r.get("rata_kepadatan", 0.0)),
                transfer=r.get("transit_count", 0),
            )
        )
    print(
        "[rute_rekomendasi] "
        f"asal={req.halte_asal} tujuan={req.halte_tujuan} "
        f"kandidat={len(rute_list)} | " + " | ".join(debug_items_pre)
    )

    hasil = []
    for r in rute_list:
        formatted = format_rute(r, graph_data)
        select_bus_per_segmen(
            formatted["segmen"], sim_time, jadwal, realtime_kepadatan
        )
        hasil.append(_apply_selected_bus_density(formatted))

    hasil.sort(key=lambda r: (
        r["density_norm"],
        r["primary_score"],
    ))

    debug_items_final = []
    for idx, r in enumerate(hasil, start=1):
        debug_items_final.append(
            "rank={rank} density_norm={density_norm:.3f} primary={primary:.4f} "
            "waktu={waktu}m jarak={jarak:.1f}m transfer={transfer} "
            "kepadatan={kepadatan:.3f} source={source}".format(
                rank=idx,
                density_norm=float(r.get("density_norm", 0.0)),
                primary=float(r.get("primary_score", 0.0)),
                waktu=r.get("estimasi_menit", 0),
                jarak=float(r.get("total_jarak_meter", 0.0)),
                kepadatan=float(r.get("rata_kepadatan", 0.0)),
                transfer=r.get("jumlah_transit", 0),
                source=r.get("density_source", "unknown"),
            )
        )
    print(
        "[rute_rekomendasi_final] "
        f"asal={req.halte_asal} tujuan={req.halte_tujuan} "
        f"kandidat={len(hasil)} | " + " | ".join(debug_items_final)
    )
    return hasil


# ----------------------------------------------------------------------
# GET /api/rute/halte
# ----------------------------------------------------------------------

@router.get("/halte")
def list_halte(request: Request) -> list[dict]:
    graph_data = request.app.state.graph_data
    halte_master: dict = graph_data["halte"]
    halte_to_koridor: dict = graph_data["halte_to_koridor"]

    hasil = []
    for halte_id, h in halte_master.items():
        koridor_list = sorted(halte_to_koridor.get(halte_id, set()))
        hasil.append({
            "halte_id": halte_id,
            "nama": h["nama"],
            "lat": h["lat"],
            "lng": h["lng"],
            "koridor_list": koridor_list,
        })
    hasil.sort(key=lambda h: h["nama"])
    return hasil


# ----------------------------------------------------------------------
# GET /api/rute/halte/{halte_id}/bus-berikutnya
# ----------------------------------------------------------------------

@router.get("/halte/{halte_id}/bus-berikutnya")
def bus_berikutnya(
    halte_id: str,
    sim_time: int,
    request: Request,
    tanggal: str | None = None,
    simulation_run_id: str | None = None,
) -> list[dict]:
    graph_data = request.app.state.graph_data
    if halte_id not in graph_data["halte"]:
        raise HTTPException(404, f"halte '{halte_id}' tidak ditemukan")

    simulation_context = getattr(request.app.state, "simulation_context", None)
    if simulation_context is not None and simulation_context.instances:
        return upcoming_buses_for_halte(
            simulation_context,
            halte_id=halte_id,
            sim_time=sim_time,
            tanggal=tanggal,
            simulation_run_id=simulation_run_id,
        )

    sb = get_client()
    rows = (
        sb.table("jadwal")
        .select("bus_id, koridor_id, waktu_tiba_detik")
        .eq("halte_id", halte_id)
        .gte("waktu_tiba_detik", sim_time)
        .lte("waktu_tiba_detik", sim_time + MAX_ETA_DETIK_DEFAULT)
        .order("waktu_tiba_detik")
        .limit(500)
        .execute()
        .data
    )

    # Untuk tiap koridor, ambil bus pertama (waktu tiba paling awal)
    per_koridor: dict[int, dict] = {}
    for r in rows:
        kid = r["koridor_id"]
        if kid in per_koridor:
            continue
        eta_detik = r["waktu_tiba_detik"] - sim_time
        per_koridor[kid] = {
            "bus_id": r["bus_id"],
            "koridor_id": kid,
            "eta_detik": eta_detik,
            "eta_menit": round(eta_detik / 60),
        }

    return sorted(per_koridor.values(), key=lambda x: x["eta_detik"])
