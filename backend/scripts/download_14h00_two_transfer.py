import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import run_monte_carlo_summary as summary

body = {
    **summary.BASE_REQUEST_BODY,
    "jam": 14,
    "sim_time": 50400,
    "routing_scenarios": [summary.SCENARIOS[2]],
}
path = Path("result/monte_carlo_raw_14h00_two-transfer_comparison.json")
request = Request(
    os.environ["MONTE_CARLO_ENDPOINT"],
    data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print("REQUEST", flush=True)
with urlopen(request, timeout=900) as response, path.open("wb") as output:
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        output.write(chunk)
print("SAVED", path, path.stat().st_size, flush=True)
