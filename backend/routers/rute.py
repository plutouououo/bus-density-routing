"""Router rekomendasi rute TransJakarta untuk kios halte."""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.bus_selector import select_bus_per_segmen
from services.dijkstra import (
    MAKS_TRANSIT_DEFAULT,
    build_graph,
    dijkstra,
    format_rute,
    get_realtime_kepadatan,
)
from services.gtfs_simulation import (
    SCOPED_KORIDOR,
    active_segment_crowding_snapshot,
    daily_mean_for,
    realtime_trip_loads,
    upcoming_buses_for_halte,
)
from services.supabase_client import get_client

router = APIRouter(prefix="/api/rute")

WIB = ZoneInfo("Asia/Jakarta")


def _jam_sekarang_wib() -> int:
    return datetime.now(WIB).hour


def _hari_tipe_sekarang() -> str:
    # Senin-Jumat (0-4) = weekday; Sabtu-Minggu (5-6) = weekend
    return "weekday" if datetime.now(WIB).weekday() < 5 else "weekend"


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
    if simulation_context is not None and simulation_context.instances:
        segment_crowding = active_segment_crowding_snapshot(
            simulation_context,
            sim_time=sim_time,
            tanggal=req.tanggal,
            simulation_run_id=req.simulation_run_id,
        )
        daily_mean_by_koridor = {}
        for kid in SCOPED_KORIDOR:
            value = daily_mean_for(simulation_context, req.tanggal, kid)
            daily_mean_by_koridor[kid] = value
            daily_mean_by_koridor[int(kid)] = value

    graph = build_graph(
        graph_data,
        jam=jam,
        hari_tipe=hari_tipe,
        segment_crowding=segment_crowding,
        daily_mean_by_koridor=daily_mean_by_koridor,
    )

    try:
        rute_list = dijkstra(graph, req.halte_asal, req.halte_tujuan, k=3)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not rute_list:
        raise HTTPException(
            404,
            f"Tidak ditemukan rute dari '{halte_master[req.halte_asal]['nama']}' "
            f"ke '{halte_master[req.halte_tujuan]['nama']}' "
            f"dalam batas {MAKS_TRANSIT_DEFAULT + 1} koridor.",
        )

    if simulation_context is not None and simulation_context.instances:
        realtime_kepadatan = realtime_trip_loads(
            simulation_context,
            tanggal=req.tanggal,
            simulation_run_id=req.simulation_run_id,
        )
    else:
        realtime_kepadatan = get_realtime_kepadatan(graph_data, jam=jam, hari_tipe=hari_tipe)

    hasil = []
    for r in rute_list:
        formatted = format_rute(r, graph_data)
        select_bus_per_segmen(
            formatted["segmen"], sim_time, jadwal, realtime_kepadatan
        )
        hasil.append(formatted)
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
