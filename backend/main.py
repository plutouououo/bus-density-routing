# backend/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from postgrest.exceptions import APIError

from routers import rute, simulation
from services.dijkstra import load_graph_data
from services.gtfs_simulation import load_simulation_context, resolve_simulation_run_id
from services.supabase_client import get_client


def _load_jadwal(sb) -> dict[str, list]:
    rows: list = []
    offset = 0
    while True:
        try:
            chunk = (
                sb.table("jadwal_dengan_koordinat")
                .select("*")
                .range(offset, offset + 999)
                .execute()
                .data
            )
        except APIError as e:
            if getattr(e, "code", None) == "PGRST205":
                print(
                    "[startup] jadwal_dengan_koordinat tidak ada; "
                    "fallback jadwal legacy dilewati"
                )
                return {}
            raise
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000

    jadwal: dict[str, list] = {}
    for r in rows:
        jadwal.setdefault(r["bus_id"], []).append(r)
    for bus_id in jadwal:
        jadwal[bus_id].sort(key=lambda s: s["urutan"])
    return jadwal


def _load_shapes(sb) -> list:
    rows: list = []
    offset = 0
    while True:
        chunk = (
            sb.table("shapes")
            .select("*")
            .order("koridor_id")
            .order("urutan")
            .range(offset, offset + 999)
            .execute()
            .data
        )
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


@asynccontextmanager
async def lifespan(app: FastAPI):
    sb = get_client()

    fallback_jadwal = _load_jadwal(sb)
    shapes = _load_shapes(sb)
    halte = sb.table("halte").select("halte_id, nama, lat, lng").execute().data

    # Data graf untuk service Dijkstra. Dimuat sekali — rebuild graf per
    # request cukup memfilter list di memory (lihat services/dijkstra.py).
    graph_data = await load_graph_data(sb)
    simulation_halte_ids = set(graph_data["halte_to_koridor"])
    simulation_context = load_simulation_context(
        sb,
        halte_rows=halte,
        segmen=graph_data["segmen"],
        fallback_jadwal=fallback_jadwal,
        allowed_halte_ids=simulation_halte_ids,
    )
    jadwal = simulation_context.jadwal or fallback_jadwal

    app.state.jadwal = jadwal
    app.state.fallback_jadwal = fallback_jadwal
    app.state.shapes = shapes
    app.state.halte = halte
    app.state.graph_data = graph_data
    app.state.simulation_context = simulation_context

    total_stops = sum(len(s) for s in jadwal.values())
    print(
        f"[startup] loaded {len(jadwal)} buses / {total_stops} stops, "
        f"{len(shapes)} shape points, {len(halte)} halte, "
        f"{len(graph_data['segmen'])} segmen, "
        f"{len(graph_data['kepadatan_bus'])} baris kepadatan_bus fallback, "
        f"{len(simulation_context.instances)} GTFS-generated trip instances, "
        f"simulation_run_id={resolve_simulation_run_id()}"
    )
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(simulation.router)
app.include_router(rute.router)
