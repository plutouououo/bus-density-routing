# backend/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import rute, simulation
from services.dijkstra import load_graph_data
from services.supabase_client import get_client


def _load_jadwal(sb) -> dict[str, list]:
    rows: list = []
    offset = 0
    while True:
        chunk = (
            sb.table("jadwal_dengan_koordinat")
            .select("*")
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

    jadwal = _load_jadwal(sb)
    shapes = _load_shapes(sb)
    halte = sb.table("halte").select("halte_id, nama, lat, lng").execute().data

    # Data graf untuk service Dijkstra. Dimuat sekali — rebuild graf per
    # request cukup memfilter list di memory (lihat services/dijkstra.py).
    graph_data = await load_graph_data(sb)

    app.state.jadwal = jadwal
    app.state.shapes = shapes
    app.state.halte = halte
    app.state.graph_data = graph_data

    total_stops = sum(len(s) for s in jadwal.values())
    print(
        f"[startup] loaded {len(jadwal)} buses / {total_stops} stops, "
        f"{len(shapes)} shape points, {len(halte)} halte, "
        f"{len(graph_data['segmen'])} segmen, "
        f"{len(graph_data['kepadatan_bus'])} baris kepadatan_bus"
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
