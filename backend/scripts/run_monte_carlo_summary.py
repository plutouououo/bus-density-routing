"""Run the Monte Carlo endpoint and write raw plus compact result files.

Run from backend/ with the project virtual environment:
    python scripts/run_monte_carlo_summary.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = os.getenv(
    "MONTE_CARLO_ENDPOINT",
    "http://127.0.0.1:8000/api/rute/monte-carlo",
)
BASE_REQUEST_BODY = {
    "tanggal": "2026-02-28",
    "jam": 8,
    "hari_tipe": "weekday",
    "sim_time": 28800,
    "master_seed": 12345,
    "replications": 100,
    "diagnostic_segment_ids": ["1_G00039_G00753"],
}
SCENARIOS = [
    {"name": "no-transfer", "halte_asal": "G00138", "halte_tujuan": "G00131"},
    {"name": "one-transfer", "halte_asal": "G00039", "halte_tujuan": "G00067"},
    {"name": "two-transfer", "halte_asal": "G00039", "halte_tujuan": "G00174"},
]
TIME_PERIODS = [
    {"label": "08h00", "jam": 8, "sim_time": 28800},
    {"label": "14h00", "jam": 14, "sim_time": 50400},
]
RESULT_DIR = Path(__file__).resolve().parents[1] / "result"


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return label or "monte_carlo"


def _route_segment_ids(scenario: dict) -> set[str]:
    segment_ids: set[str] = set()
    for replication in scenario.get("replications", []):
        for ranking in replication.get("candidate_rankings", []):
            for edge in ranking:
                if len(edge) >= 5 and edge[2] == "segmen" and edge[3] is not None:
                    segment_ids.add(str(edge[3]))
    return segment_ids


def _halte_sequence(signature) -> str:
    if not signature:
        return "(no route)"
    sequence = [str(signature[0][0])]
    sequence.extend(str(edge[1]) for edge in signature)
    return " -> ".join(sequence)


def _route_signature_text(signature) -> str:
    if signature is None:
        return "(no route)"
    return _halte_sequence(signature)


def _request_json(request_body: dict) -> dict:
    payload = json.dumps(request_body).encode("utf-8")
    request = Request(
        ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {detail[:500]}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach {ENDPOINT}: {error.reason}") from error


def _summary_lines(result: dict, request_body: dict) -> list[str]:
    load_factor = result.get("load_factor", {})
    requested_replications = request_body["replications"]
    requested_seed = str(request_body["master_seed"])
    actual_replications = load_factor.get("replication_count")
    actual_seed = str(load_factor.get("master_seed"))
    lines = [
        f"replication_count: {actual_replications} (requested {requested_replications})",
        f"master_seed: {actual_seed} (requested {requested_seed})",
    ]
    if actual_replications != requested_replications:
        lines.append("WARNING: replication_count does not match request")
    if actual_seed != requested_seed:
        lines.append("WARNING: master_seed does not match request")

    routing_sensitivity = result.get("routing_sensitivity") or {}
    scenarios = routing_sensitivity.get("scenarios", [])
    scenario_aggregates = [
        scenario.get("segment_aggregates", {}) for scenario in scenarios
    ]
    aggregates = {}
    for scenario_stats in scenario_aggregates:
        aggregates.update(scenario_stats)
    rows = sorted(
        aggregates.items(),
        key=lambda item: float(item[1].get("std_dev", 0.0)),
        reverse=True,
    )
    lines.append(f"active segment aggregates on tested routes: {len(rows)}")
    lines.append("segment_id | mean | std | p05 | p95")
    for segment_id, stats in rows[:10]:
        lines.append(
            f"{segment_id} | {stats.get('mean', 0.0):.6f} | "
            f"{stats.get('std_dev', 0.0):.6f} | {stats.get('p05', 0.0):.6f} | "
            f"{stats.get('p95', 0.0):.6f}"
        )

    for segment_id, stats in rows:
        if float(stats.get("std_dev", 0.0)) == 0.0:
            lines.append(f"WARNING: zero variance on segment {segment_id}")
        if float(stats.get("p05", 0.0)) >= float(stats.get("mean", 0.0)) or float(stats.get("mean", 0.0)) >= float(stats.get("p95", 0.0)):
            lines.append(f"WARNING: percentile inversion on segment {segment_id}")

    replications = load_factor.get("replications", [])
    if len(replications) >= 2:
        first = replications[0].get("trip_loads", {})
        second = replications[1].get("trip_loads", {})
        if first == second:
            lines.append("WARNING: replications are not varying, check seed logic")
        else:
            lines.append("reproducibility diff: replication[0] and replication[1] are DIFFERENT")
    else:
        lines.append("reproducibility diff: unavailable; fewer than 2 replications")

    for scenario in scenarios:
        lines.append(
            f"scenario {scenario.get('name', '(unnamed)')}: "
            f"stability={scenario.get('top_route_stability_rate')} "
            f"unique={scenario.get('unique_top_route_count')} "
            f"rho={scenario.get('average_spearman_rho')} "
            f"tau={scenario.get('average_kendall_tau')}"
        )
        lines.append(
            "mode route: "
            + _route_signature_text(scenario.get("top_route_mode_signature"))
        )

    diagnostic_ids = set(request_body.get("diagnostic_segment_ids", []))
    for replication in load_factor.get("replications", []):
        diagnostics = [
            row
            for row in replication.get("segment_debug", [])
            if row.get("segment_id") in diagnostic_ids and row.get("active_at_sim_time")
        ]
        if not diagnostics:
            continue
        lines.append(
            f"diagnostic replication {replication.get('replication_index')}: "
            f"{len(diagnostics)} active target occurrence(s)"
        )
        for row in diagnostics:
            lines.append(
                f"  {row['trip_instance_id']} {row['segment_id']}: "
                f"lambda_trip={row['trip_poisson_lambda']:.6f}, "
                f"Rtrip_raw={row['trip_poisson_draw_raw']}, "
                f"Rtrip={row['trip_scaled_weight']:.6f}, "
                f"lambda_segment={row['lambda']:.6f}, "
                f"Rsegment_raw={row['poisson_draw_raw']}, "
                f"Rsegment={row['poisson_draw']:.6f}, "
                f"raw={row['pre_normalization_raw_weight']:.6f}, "
                f"avg_raw={row['average_pre_normalization_raw_weight']:.6f}, "
                f"scale={row['normalization_scale']:.6f}, "
                f"final={row['final_segment_load_factor']:.6f}"
            )
    return lines


def main() -> int:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for period in TIME_PERIODS:
        for scenario in SCENARIOS:
            request_body = {
                **BASE_REQUEST_BODY,
                "jam": period["jam"],
                "sim_time": period["sim_time"],
                "routing_scenarios": [scenario],
            }
            label = f"{period['label']}_{_safe_label(scenario['name'])}"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_path = RESULT_DIR / f"monte_carlo_raw_{label}_{timestamp}.json"
            summary_path = RESULT_DIR / f"{label}_{timestamp}.txt"

            try:
                result = _request_json(request_body)
            except RuntimeError as error:
                print(f"ERROR [{label}]: {error}", file=sys.stderr)
                failures += 1
                continue

            raw_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
            lines = _summary_lines(result, request_body)
            summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"\n===== {label} =====")
            print(f"raw JSON: {raw_path}")
            print(f"summary: {summary_path}")
            print("\n".join(lines))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
