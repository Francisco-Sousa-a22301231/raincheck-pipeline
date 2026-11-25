"""Timeline stitching and tile building (pure: no I/O, no third-party imports).

Tile format: see the README ("Tile format"); it is the contract with the Rain Check Worker.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Callable, Iterator

from . import physics

HOUR = 3600
STEP_H = 3  # hours per step
SPAN_H = 72  # hours of past and of future in a tile
RUN_EVERY_H = 6  # IFS runs at 00, 06, 12 and 18 UTC
MODEL = "ifs025"
TILE_VERSION = 1

# GRIB short names of the seven fields.
PARAMS = ("tp", "2t", "2d", "10u", "10v", "tcc", "ssrd")

# The fields that are a state at the end of a step (the other two, tp and ssrd, add up from the run's start).
STATE_PARAMS = ("2t", "2d", "10u", "10v", "tcc")

# (run, step, param) -> value at one point, or None when that field is missing.
Getter = Callable[[int, int, str], "float | None"]


def tile_key(lat: float, lon: float) -> str:
    """The 1-degree tile a point falls in: `<floor(lat)>_<floor(lon)>`."""
    return f"{math.floor(lat)}_{math.floor(lon)}"


def timeline(run: int) -> list[int]:
    """End of each 3-hour step (unix s), from 72 h before the run (first step ends at -69 h) to 72 h after."""
    return [run + h * HOUR for h in range(-SPAN_H + STEP_H, SPAN_H + STEP_H, STEP_H)]


def future_steps() -> list[int]:
    """Steps of the newest run that are needed (hours): 0, 3, ... 72."""
    return list(range(0, SPAN_H + STEP_H, STEP_H))


def earlier_runs(run: int) -> list[int]:
    """The earlier runs whose first 6 hours rebuild the past 72 h (oldest first)."""
    return [run - h * HOUR for h in range(SPAN_H, 0, -RUN_EVERY_H)]


EARLIER_STEPS = (3, 6)


def holes(run: int, have: dict[tuple[int, int], set[str]]) -> list[str]:
    """What the newest run lacks, as text (empty when it is whole): a run with holes must not be uploaded.

    `have[(run, step)]` is the set of fields that were read for that step. Every step from 0 to 72 h has
    to be there with all seven fields; at step 0 the two accumulated ones are not used (they are zero).
    """
    out: list[str] = []
    for step in future_steps():
        fields = have.get((run, step))
        if fields is None:
            out.append(f"step {step}")
            continue
        lacking = [p for p in (STATE_PARAMS if step == 0 else PARAMS) if p not in fields]
        if lacking:
            out.append(f"step {step}: {', '.join(lacking)}")
    return out


def source_of(run: int, t: int) -> tuple[int, int]:
    """Which (run, step) covers the step ending at `t`.

    After the run: the newest run itself. At or before it: the run that started 3 or 6 hours before `t`
    (its step 3 or 6), i.e. always the freshest forecast that has that step.
    """
    if t > run:
        return run, (t - run) // HOUR
    behind = (run - t) // HOUR
    step = 6 if behind % RUN_EVERY_H == 0 else 3
    return t - step * HOUR, step


def _clean(value: float | None, digits: int) -> float | int | None:
    if value is None or not math.isfinite(value):
        return None
    r = round(value, digits)
    return int(r) if r == int(r) else r


def point_series(run: int, lat: float, lon: float, get: Getter, cloud_scale: float = 1.0) -> dict[str, list]:
    """The six series of one point over `timeline(run)`.

    `get(run, step, param)` returns the raw GRIB value sampled at the point (tp in m and ssrd in J m-2,
    both accumulated since the run started; 2t and 2d in K; 10u and 10v in m/s; tcc in % or 0..1) or
    None. `cloud_scale` turns tcc into % (1 when the file already holds %, 100 when it holds 0..1).
    """
    times = timeline(run)
    n = len(times)
    rain: list[float | None] = [None] * n
    solar: list[float | None] = [None] * n  # MJ m-2 in the step
    temp: list[float | None] = [None] * n
    dew: list[float | None] = [None] * n
    wind_ms: list[float | None] = [None] * n
    cloud: list[float | None] = [None] * n

    def amount(src_run: int, step: int, param: str) -> float | None:
        # Accumulated since the run started -> inside (step - 3 h, step].
        prev = 0.0 if step == STEP_H else get(src_run, step - STEP_H, param)
        return physics.per_step([prev, get(src_run, step, param)], starts_at_zero=False)[1]

    for k, t in enumerate(times):
        src_run, step = source_of(run, t)
        tp = amount(src_run, step, "tp")
        rain[k] = None if tp is None else tp * 1000.0
        ssrd = amount(src_run, step, "ssrd")
        solar[k] = None if ssrd is None else ssrd / 1e6
        # State at the end of the step. At the run time itself the new run's analysis (step 0) is the
        # best there is; the 6-hour forecast of the run before stands in when it is missing.
        states = [(run, 0), (src_run, step)] if t == run else [(src_run, step)]
        for s_run, s_step in states:
            if temp[k] is None:
                v = get(s_run, s_step, "2t")
                temp[k] = None if v is None else v - 273.15
            if dew[k] is None:
                v = get(s_run, s_step, "2d")
                dew[k] = None if v is None else v - 273.15
            if wind_ms[k] is None:
                u, v = get(s_run, s_step, "10u"), get(s_run, s_step, "10v")
                wind_ms[k] = None if u is None or v is None else math.hypot(u, v)
            if cloud[k] is None:
                v = get(s_run, s_step, "tcc")
                cloud[k] = None if v is None else min(100.0, max(0.0, v * cloud_scale))

    et0: list[float | None] = [None] * n
    for k, t in enumerate(times):
        if solar[k] is None or temp[k] is None or dew[k] is None or wind_ms[k] is None:
            continue
        # Mean state of the step: its end and its start (the end of the step before), when both exist.
        j = k - 1 if k > 0 and temp[k - 1] is not None and dew[k - 1] is not None and wind_ms[k - 1] is not None else k
        t_mean = (temp[k] + temp[j]) / 2.0
        ea = (physics.saturation_vapour_pressure(dew[k]) + physics.saturation_vapour_pressure(dew[j])) / 2.0
        u2 = (wind_ms[k] + wind_ms[j]) / 2.0 * physics.WIND_10M_TO_2M
        clouds = [c for c in (cloud[k], cloud[j]) if c is not None]
        et0[k] = physics.et0_fao56(
            t_c=t_mean, ea_kpa=ea, u2_ms=u2, rs_mj=solar[k], lat=lat, lon=lon,
            mid_utc=datetime.fromtimestamp(t - STEP_H * HOUR / 2, tz=timezone.utc), hours=STEP_H,
            cloud=sum(clouds) / len(clouds) / 100.0 if clouds else None,
        )

    return {
        "rain": [_clean(v, 2) for v in rain],
        "et0": [_clean(v, 2) for v in et0],
        "temp": [_clean(v, 1) for v in temp],
        "rh": [_clean(physics.relative_humidity(a, b), 0) for a, b in zip(temp, dew)],
        "wind": [_clean(None if v is None else v * 3.6, 1) for v in wind_ms],
        "cloud": [_clean(v, 0) for v in cloud],
    }


def chance_on_timeline(run: int, per_step: dict[int, float | None]) -> list[int | None]:
    """The ENS rain chance (`per_step[step]`, %, step = hours after the run) over `timeline(run)`.

    Only the steps after the run have one (it comes from the new run's members); the past and any step
    without a value are null: the server then uses its own estimate for those.
    """
    out: list[int | None] = []
    for t in timeline(run):
        v = per_step.get((t - run) // HOUR) if t > run else None
        out.append(None if v is None or not math.isfinite(v) else int(round(min(100.0, max(0.0, v)))))
    return out


def build_tiles(cells: list[dict], series: list[dict[str, list]], run: int, made: int) -> dict[str, dict]:
    """Groups the points' series into 1-degree tiles: `{ key: tile }`. `series[i]` belongs to `cells[i]`."""
    times = timeline(run)
    tiles: dict[str, dict] = {}
    for cell, s in zip(cells, series):
        key = tile_key(cell["lat"], cell["lon"])
        tile = tiles.setdefault(key, {"v": TILE_VERSION, "model": MODEL, "run": run, "made": made, "t": times, "zones": {}})
        tile["zones"][cell["id"]] = s
    return tiles


def encode(obj: object) -> bytes:
    """Compact JSON; refuses NaN/Infinity (missing values must be null)."""
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")


def batches(tiles: dict[str, dict], max_tiles: int = 60, max_bytes: int = 128 * 1024) -> Iterator[dict[str, dict]]:
    """Splits the tiles into upload batches: at most `max_tiles` each and about `max_bytes` of JSON.

    A single tile bigger than `max_bytes` still goes out, alone. Keys keep a stable (sorted) order.
    """
    batch: dict[str, dict] = {}
    size = 0
    for key in sorted(tiles):
        n = len(encode(tiles[key])) + len(key) + 4
        if batch and (len(batch) >= max_tiles or size + n > max_bytes):
            yield batch
            batch, size = {}, 0
        batch[key] = tiles[key]
        size += n
    if batch:
        yield batch
