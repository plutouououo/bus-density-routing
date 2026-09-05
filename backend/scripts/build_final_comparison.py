import asyncio
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.dijkstra import load_graph_data
from services.geo import distance_meters
from services.supabase_client import get_client

ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = ROOT / "backend" / "result"
OUTPUT = ROOT / "results" / "final_comparison_table.csv"
FILES = [
    ("08h00", "no-transfer", "monte_carlo_raw_08h00_no-transfer_20260813_195331.json"),
    ("08h00", "one-transfer", "monte_carlo_raw_08h00_one-transfer_20260813_195426.json"),
    ("08h00", "two-transfer", "monte_carlo_raw_08h00_two-transfer_20260813_195521.json"),
    ("14h00", "no-transfer", "monte_carlo_raw_14h00_no-transfer_comparison.json"),
    ("14h00", "one-transfer", "monte_carlo_raw_14h00_one-transfer_comparison.json"),
    ("14h00", "two-transfer", "monte_carlo_raw_14h00_two-transfer_comparison.json"),
]
FIELDNAMES = [
    "scenario_type", "time_period", "origin", "destination",
    "baseline_route_signature", "baseline_density", "baseline_time",
    "baseline_distance", "baseline_transfers", "recommended_route_signature",
    "recommended_density", "recommended_time", "recommended_distance",
    "recommended_transfers", "delta_density", "delta_time", "delta_distance",
    "delta_transfers", "is_better", "route_changed", "stability_rate",
    "unique_top_route_count", "highest_variance_segment_id", "segment_mean",
    "segment_std", "segment_p05", "segment_p95",
]


def route_text(signature):
    if not signature:
        return ""
    nodes = [str(signature[0][0])]
    nodes.extend(str(edge[1]) for edge in signature)
    return " -> ".join(nodes)


def route_metrics(signature, graph_data):
    if not signature:
        return {"time": 0.0, "distance": 0.0, "transfers": 0}
    segmen_by_id = {str(row["segmen_id"]): row for row in graph_data["segmen"]}
    total_time = 0.0
    total_distance = 0.0
    transfers = 0
    for edge in signature:
        if edge[2] == "transit":
            transfers += 1
            continue
        segmen = segmen_by_id.get(str(edge[3]))
        if not segmen:
            continue
        total_time += float(segmen.get("waktu_tempuh_detik", 0) or 0)
        asal = graph_data["halte"].get(str(edge[0]))
        tujuan = graph_data["halte"].get(str(edge[1]))
        if asal and tujuan:
            total_distance += distance_meters(
                asal["lat"], asal["lng"], tujuan["lat"], tujuan["lng"]
            )
    return {
        "time": round(total_time / 60.0, 3),
        "distance": round(total_distance, 3),
        "transfers": transfers,
    }


async def main():
    graph_data = await load_graph_data(get_client())
    rows = []
    for time_period, scenario_type, filename in FILES:
        data = json.loads((RESULT_DIR / filename).read_text(encoding="utf-8"))
        scenario = data["routing_sensitivity"]["scenarios"][0]
        baseline_signature = scenario["baseline_route_signature"]
        recommended_signature = scenario["recommended_route_signature"]
        baseline = route_metrics(baseline_signature, graph_data)
        recommended = route_metrics(recommended_signature, graph_data)
        segment_rows = scenario.get("segment_aggregates", {})
        highest_segment_id, highest_stats = max(
            segment_rows.items(),
            key=lambda item: float(item[1].get("std_dev", 0.0)),
        )
        baseline_density = float(scenario["baseline_density_mean"])
        recommended_density = float(scenario["recommended_density_mean"])
        rows.append({
            "scenario_type": scenario_type,
            "time_period": time_period,
            "origin": scenario["halte_asal"],
            "destination": scenario["halte_tujuan"],
            "baseline_route_signature": route_text(baseline_signature),
            "baseline_density": round(baseline_density, 6),
            "baseline_time": baseline["time"],
            "baseline_distance": baseline["distance"],
            "baseline_transfers": baseline["transfers"],
            "recommended_route_signature": route_text(recommended_signature),
            "recommended_density": round(recommended_density, 6),
            "recommended_time": recommended["time"],
            "recommended_distance": recommended["distance"],
            "recommended_transfers": recommended["transfers"],
            "delta_density": round(baseline_density - recommended_density, 6),
            "delta_time": round(recommended["time"] - baseline["time"], 3),
            "delta_distance": round(recommended["distance"] - baseline["distance"], 3),
            "delta_transfers": recommended["transfers"] - baseline["transfers"],
            "is_better": recommended_density < baseline_density,
            "route_changed": baseline_signature != recommended_signature,
            "stability_rate": scenario["top_route_stability_rate"],
            "unique_top_route_count": scenario["unique_top_route_count"],
            "highest_variance_segment_id": highest_segment_id,
            "segment_mean": round(float(highest_stats["mean"]), 6),
            "segment_std": round(float(highest_stats["std_dev"]), 6),
            "segment_p05": round(float(highest_stats["p05"]), 6),
            "segment_p95": round(float(highest_stats["p95"]), 6),
        })

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(OUTPUT)
    print("R_better=" + str(sum(row["is_better"] for row in rows) / len(rows) * 100))
    print(OUTPUT.read_text(encoding="utf-8"))


asyncio.run(main())
