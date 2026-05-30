from collections import defaultdict
from fastapi import APIRouter, Request

from services.gtfs_simulation import get_active_positions
from services.interpolation import get_bus_position

router = APIRouter(prefix="/api/simulation")


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
    grouped: dict[int, list] = defaultdict(list)
    for row in shapes:
        grouped[row["koridor_id"]].append([row["lng"], row["lat"]])
    return {str(k): v for k, v in grouped.items()}


@router.get("/halte")
def get_halte(request: Request):
    return request.app.state.halte
