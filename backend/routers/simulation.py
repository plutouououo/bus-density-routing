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
    return request.app.state.halte
