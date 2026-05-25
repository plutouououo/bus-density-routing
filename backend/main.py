# backend/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import simulation
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    sb = get_client()

    jadwal = _load_jadwal(sb)
    shapes = (
        sb.table("shapes")
        .select("*")
        .order("koridor_id")
        .order("urutan")
        .execute()
        .data
    )
    halte = sb.table("halte").select("halte_id, nama, lat, lng").execute().data

    app.state.jadwal = jadwal
    app.state.shapes = shapes
    app.state.halte = halte

    total_stops = sum(len(s) for s in jadwal.values())
    print(
        f"[startup] loaded {len(jadwal)} buses / {total_stops} stops, "
        f"{len(shapes)} shape points, {len(halte)} halte"
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
