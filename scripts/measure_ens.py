"""Measures the ENS rain chance at a list of places: what it costs, and how it compares with Open-Meteo.

    python scripts/measure_ens.py POINTS.json [--run YYYYMMDDHH] [--om N]

Prints the bytes, requests, seconds and peak memory of the ENS part of a run (the same code the job runs:
cli.extract_ens), how far the chance is from the soft chance the server makes from HRES rain alone
(Rain Check's src/slim.ts softChance), and with --om N Open-Meteo's ecmwf_ifs025 precipitation_probability
at N wet places (N requests), as the highest hourly chance in each 3-hour step. Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raincheck_pipeline import cli, ecmwf, ens  # noqa: E402
from raincheck_pipeline.grib import Sampler  # noqa: E402
from raincheck_pipeline.http import counter, request  # noqa: E402
from raincheck_pipeline.tiles import HOUR  # noqa: E402

SOFT_MIN, SOFT_MAX, SOFT_MM = 10, 85, 1.0  # Rain Check src/slim.ts


def soft_chance(rain: list[float]) -> list[int]:
    out = []
    for k, r in enumerate(rain):
        nb = max(rain[k - 1] if k > 0 else 0.0, rain[k + 1] if k + 1 < len(rain) else 0.0)
        out.append(round(SOFT_MIN + (SOFT_MAX - SOFT_MIN) * (1 - math.exp(-max(0.0, max(r, nb / 2)) / SOFT_MM))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("points")
    ap.add_argument("--run")
    ap.add_argument("--om", type=int, default=0)
    args = ap.parse_args()
    threads = int(__import__("os").environ.get("DOWNLOAD_THREADS", "8"))

    cells = json.loads(Path(args.points).read_text(encoding="utf-8"))
    points = sorted({(c["lat"], c["lon"]) for c in cells})
    if args.run:
        run = int(datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp())
    else:
        run, with_ens = cli.newest_run(int(time.time()))
        if run is None or not with_ens:
            print("no run with its ENS out")
            return 1
    print(f"run {ecmwf.run_name(run)}; {len(points)} points; {threads} threads")
    sampler = Sampler(points)
    mask = (ecmwf.step_fields(run, 0, params=("lsm",)) or {}).get("lsm")
    if mask:
        sampler.set_land_mask(ecmwf.download(mask))
    steps = ens.steps()
    hres = {}
    for s in steps:
        f = (ecmwf.step_fields(run, s, params=("tp",)) or {}).get("tp")
        hres[s] = sampler.sample(ecmwf.download(f))[1] if f else None

    b0, r0, t0 = counter.bytes, counter.requests, time.monotonic()
    chance = cli.extract_ens(run, sampler, threads)
    dt = time.monotonic() - t0
    print(f"ENS: {(counter.bytes - b0) / 1e6:.1f} MB in {counter.requests - r0} requests, {dt:.0f} s; "
          f"peak memory {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")

    gaps, wet = [], []
    for i, _ in enumerate(points):
        acc = [0.0] + [float(hres[s][i]) * 1000 if hres[s] is not None else 0.0 for s in steps]
        soft = soft_chance([max(0.0, b - a) for a, b in zip(acc, acc[1:])])
        p = [float(chance[k][i]) for k in range(len(steps))]
        gaps += [abs(a - b) for a, b in zip(soft, p) if b == b]
        if max(p) >= 50:
            wet.append(i)
    print(f"mean |soft chance - ENS chance|: {sum(gaps) / max(1, len(gaps)):.1f} points over {len(gaps)} steps")

    tl = [run + s * HOUR for s in steps]
    for i in wet[: args.om]:
        lat, lon = points[i]
        q = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&cell_selection=land"
             f"&hourly=precipitation_probability&models=ecmwf_ifs025&timeformat=unixtime&timezone=GMT"
             f"&start_hour={time.strftime('%Y-%m-%dT%H:00', time.gmtime(run + HOUR))}"
             f"&end_hour={time.strftime('%Y-%m-%dT%H:00', time.gmtime(run + 72 * HOUR))}")
        h = json.loads(request(q, user_agent="raincheck-pipeline/1.0")[1])["hourly"]
        by = dict(zip(h["time"], h["precipitation_probability"]))
        om = [max((by.get(t - k * HOUR) or 0) for k in range(3)) for t in tl]
        print(f"{lat}, {lon}\n  ENS " + " ".join(f"{chance[k][i]:3.0f}" for k in range(len(steps)))
              + "\n  OM  " + " ".join(f"{x:3d}" for x in om))
    return 0


if __name__ == "__main__":
    sys.exit(main())
