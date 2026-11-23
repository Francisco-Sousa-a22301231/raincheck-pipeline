"""Plausibility check: a dry run's tiles against Open-Meteo's copy of the same model (ecmwf_ifs025).

    python scripts/compare_open_meteo.py points.example.json out

One Open-Meteo request per point. Open-Meteo interpolates IFS to hourly values and to the exact place
(and corrects temperature for altitude), so the two never match exactly: the numbers to look at are
the mean absolute difference and the totals. Standard library only.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

HOURLY = "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,cloud_cover,et0_fao_evapotranspiration"
# tile name -> (Open-Meteo name, True when the value is an amount summed over the 3 hours)
PAIRS = {
    "rain": ("precipitation", True),
    "et0": ("et0_fao_evapotranspiration", True),
    "temp": ("temperature_2m", False),
    "rh": ("relative_humidity_2m", False),
    "wind": ("wind_speed_10m", False),
    "cloud": ("cloud_cover", False),
}


def open_meteo(lat: float, lon: float) -> dict[str, dict[int, float | None]]:
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly={HOURLY}"
        "&models=ecmwf_ifs025&past_days=4&forecast_days=5&timeformat=unixtime&timezone=GMT&wind_speed_unit=kmh"
    )
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "open-data-check/1.0"}), timeout=60) as res:
        hourly = json.load(res)["hourly"]
    return {name: dict(zip(hourly["time"], hourly[name])) for name in HOURLY.split(",")}


def at(series: dict[int, float | None], t: int, summed: bool) -> float | None:
    if not summed:
        return series.get(t)
    hours = [series.get(t - k * 3600) for k in range(3)]  # an hourly amount belongs to the hour before
    return None if any(h is None for h in hours) else sum(hours)


def main() -> int:
    points = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    for p in points:
        key = f"{math.floor(p['lat'])}_{math.floor(p['lon'])}"
        tile = json.loads((out / f"{key}.json").read_text(encoding="utf-8"))
        zone = tile["zones"][p["id"]]
        om = open_meteo(p["lat"], p["lon"])
        print(f"\n{p['id']} ({p['lat']}, {p['lon']}), run {tile['run']}: ours vs Open-Meteo")
        print(f"  {'':6} {'n':>3} {'mean ours':>10} {'mean OM':>9} {'mean |diff|':>12} {'max |diff|':>11}   past / future mean |diff|")
        for name, (om_name, summed) in PAIRS.items():
            rows = [(t, a, at(om[om_name], t, summed)) for t, a in zip(tile["t"], zone[name])]
            rows = [(t, a, b) for t, a, b in rows if a is not None and b is not None]
            if not rows:
                print(f"  {name:6} no overlap")
                continue
            diffs = [abs(a - b) for _, a, b in rows]
            past = [abs(a - b) for t, a, b in rows if t <= tile["run"]]
            future = [abs(a - b) for t, a, b in rows if t > tile["run"]]
            mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
            extra = f"   total {sum(a for _, a, _ in rows):.1f} vs {sum(b for _, _, b in rows):.1f} mm" if summed else ""
            print(f"  {name:6} {len(rows):3d} {mean([a for _, a, _ in rows]):10.2f} {mean([b for _, _, b in rows]):9.2f} "
                  f"{mean(diffs):12.2f} {max(diffs):11.2f}   {mean(past):.2f} / {mean(future):.2f}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
