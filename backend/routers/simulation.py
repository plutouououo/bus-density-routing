from collections import defaultdict
from fastapi import APIRouter, Request

from services.interpolation import get_bus_position

router = APIRouter(prefix="/api/simulation")


@router.get("/positions")
def get_positions(request: Request, sim_time: int):
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
