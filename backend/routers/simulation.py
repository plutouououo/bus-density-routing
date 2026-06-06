from collections import defaultdict
from fastapi import APIRouter, Request

from services.geo import distance_meters
from services.gtfs_simulation import get_active_positions
from services.interpolation import get_bus_position

router = APIRouter(prefix="/api/simulation")

HALTE_NUMBERED_DUPLICATE_RADIUS_METER = 120


def _base_halte_name(value) -> str:
    parts = str(value or "").strip().split()
    if parts and parts[-1].isdigit():
        return " ".join(parts[:-1])
    return " ".join(parts)


def _is_numbered_halte_name(value) -> bool:
    parts = str(value or "").strip().split()
    return bool(parts and parts[-1].isdigit())


def _hide_generic_numbered_duplicates(halte: list[dict]) -> list[dict]:
    numbered_by_base: dict[str, list[dict]] = defaultdict(list)
    for row in halte:
        if _is_numbered_halte_name(row.get("nama")):
            numbered_by_base[_base_halte_name(row.get("nama")).lower()].append(row)

    if not numbered_by_base:
        return halte

    hasil = []
    for row in halte:
        base_name = _base_halte_name(row.get("nama"))
        if base_name != str(row.get("nama") or "").strip():
            hasil.append(row)
            continue

        numbered_matches = numbered_by_base.get(base_name.lower(), [])
        should_hide = any(
            distance_meters(row["lat"], row["lng"], other["lat"], other["lng"])
            <= HALTE_NUMBERED_DUPLICATE_RADIUS_METER
            for other in numbered_matches
        )
        if not should_hide:
            hasil.append(row)
    return hasil


@router.get("/positions")
def get_positions(
    request: Request,
    sim_time: int,
    tanggal: str | None = None,
    simulation_run_id: str | None = None,
    max_visible_per_corridor_direction: int | None = None,
):
    simulation_context = getattr(request.app.state, "simulation_context", None)
    if simulation_context is not None and simulation_context.instances:
        positions = get_active_positions(
            simulation_context,
            sim_time=sim_time,
            tanggal=tanggal,
            simulation_run_id=simulation_run_id,
            max_visible_per_corridor_direction=max_visible_per_corridor_direction,
        )
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [pos["lng"], pos["lat"]]},
                    "properties": {
                        "bus_id": pos["bus_id"],
                        "koridor_id": pos["koridor_id"],
                        "bearing": pos["bearing"],
                        "next_stop": pos["next_stop"],
                        "eta_minutes": pos["eta_minutes"],
                        "trip_id": pos["trip_id"],
                        "trip_instance_id": pos["trip_instance_id"],
                        "direction_id": pos["direction_id"],
                        "trip_load_factor": pos["trip_load_factor"],
                        "label_kepadatan": pos["label_kepadatan"],
                        "kategori_kepadatan": pos["kategori_kepadatan"],
                        "status": pos["status"],
                        "estimated_passengers": pos["estimated_passengers"],
                        "capacity": pos["capacity"],
                    },
                }
                for pos in positions
            ],
        }

    jadwal: dict = request.app.state.jadwal
    features = []
    for bus_id, stops in jadwal.items():
        pos = get_bus_position(bus_id, stops, sim_time)
        if pos is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [pos["lng"], pos["lat"]]},
            "properties": {
                "bus_id": pos["bus_id"],
                "koridor_id": pos["koridor_id"],
                "bearing": pos["bearing"],
                "next_stop": pos["next_stop"],
                "eta_minutes": pos["eta_minutes"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


@router.get("/shapes")
def get_shapes(request: Request):
    shapes = request.app.state.shapes
    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in shapes:
        shape_id = str(row.get("shape_id") or "default")
        grouped[(row["koridor_id"], shape_id)].append(row)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [row["lng"], row["lat"]]
                        for row in sorted(
                            rows,
                            key=lambda r: r.get("shape_pt_sequence", r.get("urutan", 0)),
                        )
                    ],
                },
                "properties": {
                    "koridor_id": koridor_id,
                    "shape_id": shape_id,
                },
            }
            for (koridor_id, shape_id), rows in grouped.items()
            if len(rows) >= 2
        ],
    }


@router.get("/halte")
def get_halte(request: Request):
    graph_data = getattr(request.app.state, "graph_data", None)
    if not graph_data:
        return request.app.state.halte

    connected_halte_ids = set(graph_data.get("halte_to_koridor", {}))

    halte = [
        h
        for h in request.app.state.halte
        if str(h.get("halte_id")) in connected_halte_ids
    ]
    halte = _hide_generic_numbered_duplicates(halte)
    return sorted(halte, key=lambda h: (h.get("nama") or "", str(h.get("halte_id"))))
