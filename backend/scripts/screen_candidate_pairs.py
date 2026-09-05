import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.dijkstra import KANDIDAT_RUTE_DEFAULT, build_graph, dijkstra, load_graph_data
from services.supabase_client import get_client


def signature(route):
    return " -> ".join([route["path"][0]["asal"]] + [edge["tujuan"] for edge in route["path"]])


def primary_close(scores):
    if len(scores) < 2:
        return False
    best = max(abs(scores[0]), 1e-9)
    return any(abs(score - scores[0]) / best <= 0.15 for score in scores[1:])


async def main():
    data = await load_graph_data(get_client())
    graph = build_graph(data, jam=8, hari_tipe="weekday")
    members = {}
    for row in data["koridor_halte"]:
        kid = str(row.get("koridor_id"))
        if kid in {"1", "2", "3", "4", "5"}:
            members.setdefault(kid, []).append(str(row["halte_id"]))

    candidates = []
    for kid, halte_ids in sorted(members.items()):
        unique = list(dict.fromkeys(halte_ids))
        for left in range(0, len(unique), max(1, len(unique) // 8)):
            for right in range(left + 2, len(unique), max(1, len(unique) // 8)):
                routes = dijkstra(graph, unique[left], unique[right], k=KANDIDAT_RUTE_DEFAULT)
                if len(routes) < 2:
                    continue
                scores = [float(route.get("primary_score", 0.0)) for route in routes]
                densities = [
                    float(route.get("rata_kepadatan", 0.0))
                    for route in routes
                ]
                close_gap = min(
                    abs(scores[index] - scores[0]) / max(abs(scores[0]), 1e-9)
                    for index in range(1, len(scores))
                )
                density_gap = max(densities) - min(densities)
                candidates.append((close_gap, density_gap, unique[left], unique[right], scores, densities, routes))

    candidates.sort(key=lambda item: (item[0] <= 0.15, item[1], -item[0]), reverse=True)
    for close_gap, density_gap, origin, destination, scores, densities, routes in candidates[:20]:
        print(f"OD {origin} -> {destination} candidates={len(routes)}")
        print(f"  primary_gap={close_gap:.3f} density_gap={density_gap:.3f}")
        for index, route in enumerate(routes, 1):
            print(
                f"  {index}: primary={scores[index - 1]:.6f} "
                f"density={densities[index - 1]:.6f} "
                f"time={route.get('total_waktu_detik', 0) / 60:.2f} "
                f"transfers={route.get('transit_count', 0)} "
                f"route={signature(route)}"
            )


asyncio.run(main())
