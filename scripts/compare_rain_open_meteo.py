"""Plausibility check: a rain dry run's tiles against Open-Meteo's copies of the same models.

    python scripts/compare_rain_open_meteo.py points.example.json out-rain [N]

One Open-Meteo forecast request per place (the first N places, default 4) for GFS (`gfs_seamless`),
ICON-EU (`icon_eu`) and AIFS (`ecmwf_aifs025_single`), and one ensemble request for GEFS (`gfs025`, 31
members) at the first place. Open-Meteo interpolates to the exact place and to hourly values (and over
Europe its "seamless" GFS may blend other NOAA grids), so the numbers never match exactly: look at the
totals and at whether the wet steps line up. Standard library only.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import urllib.request
from pathlib import Path

OM_MODELS = {"gfs": "gfs_seamless", "icon_eu": "icon_eu", "aifs": "ecmwf_aifs025_single"}
UA = {"User-Agent": "open-data-check/1.0"}


def fetch(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as res:
        return json.load(res)


def three_hours(hourly: dict[int, float | None], t: int) -> float | None:
    """The amount in the 3 hours ending at `t` (Open-Meteo's hourly amount belongs to the hour before)."""
    hours = [hourly.get(t - k * 3600) for k in range(3)]
    return None if any(h is None for h in hours) else sum(hours)


def main() -> int:
    points = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    for k, p in enumerate(points[:n]):
        key = f"{math.floor(p['lat'])}_{math.floor(p['lon'])}"
        tile = json.loads((out / f"{key}.json").read_text(encoding="utf-8"))
        zone = tile["zones"][p["id"]]
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={p['lat']}&longitude={p['lon']}&hourly=precipitation"
               f"&models={','.join(OM_MODELS.values())}&forecast_days=5&timeformat=unixtime&timezone=GMT")
        hourly = fetch(url)["hourly"]
        print(f"\n{p['id']} ({p['lat']}, {p['lon']}): ours vs Open-Meteo, mm per 3 h over the tile's {len(tile['t'])} steps")
        for name, om in OM_MODELS.items():
            ours = zone.get(name)
            series = hourly.get(f"precipitation_{om}")
            if ours is None or series is None:
                print(f"  {name:8} not in both")
                continue
            by_t = dict(zip(hourly["time"], series))
            rows = [(t, a, three_hours(by_t, t)) for t, a in zip(tile["t"], ours)]
            rows = [(t, a, b) for t, a, b in rows if a is not None and b is not None]
            if not rows:
                print(f"  {name:8} no overlap")
                continue
            diffs = [abs(a - b) for _, a, b in rows]
            wet_ours = {t for t, a, _ in rows if a >= 0.3}
            wet_om = {t for t, _, b in rows if b >= 0.3}
            both = len(wet_ours & wet_om)
            print(f"  {name:8} n={len(rows):2d} total {sum(a for _, a, _ in rows):6.1f} vs {sum(b for _, _, b in rows):6.1f} mm"
                  f"   mean |diff| {statistics.mean(diffs):.2f}  max {max(diffs):.1f}"
                  f"   wet steps (>=0.3) ours {len(wet_ours)}, OM {len(wet_om)}, both {both}")
            print("           ours " + " ".join("  ." if a is None else f"{a:3.1f}" for _, a, _ in rows))
            print("           OM   " + " ".join(f"{b:3.1f}" for _, _, b in rows))
        if k == 0 and "gefs_p" in zone:
            url = (f"https://ensemble-api.open-meteo.com/v1/ensemble?latitude={p['lat']}&longitude={p['lon']}"
                   "&hourly=precipitation&models=gfs025&forecast_days=5&timeformat=unixtime&timezone=GMT")
            ens = fetch(url)["hourly"]
            members = [v for name, v in ens.items() if name.startswith("precipitation")]
            chance = []
            for t in tile["t"]:
                amounts = [three_hours(dict(zip(ens["time"], m)), t) for m in members]
                amounts = [a for a in amounts if a is not None]
                chance.append(round(100 * sum(a >= 0.3 for a in amounts) / len(amounts)) if amounts else None)
            rows = [(a, b) for a, b in zip(zone["gefs_p"], chance) if a is not None and b is not None]
            if rows:
                print(f"  gefs_p   {len(members)} OM members; mean |diff| {statistics.mean(abs(a - b) for a, b in rows):.1f} points")
                print("           ours " + " ".join(f"{a:3d}" for a, _ in rows))
                print("           OM   " + " ".join(f"{b:3d}" for _, b in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
