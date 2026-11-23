import json
import re

import pytest

from raincheck_pipeline import tiles
from raincheck_pipeline.tiles import HOUR

RUN = 1795392000  # 2026-11-23 00 UTC
KEY_RE = re.compile(r"^-?\d{1,2}_-?\d{1,3}$")  # what the server accepts


def test_tile_key():
    assert tiles.tile_key(38.6982, -9.3812) == "38_-10"
    assert tiles.tile_key(-33.9, 151.2) == "-34_151"
    assert tiles.tile_key(0.5, -0.5) == "0_-1"
    assert tiles.tile_key(-0.5, 0.5) == "-1_0"
    assert tiles.tile_key(64.0, -22.0) == "64_-22"
    for lat, lon in [(90, 180), (-90, -180), (38.7, -9.4), (-89.99, 179.99)]:
        assert KEY_RE.match(tiles.tile_key(lat, lon))


def test_timeline():
    t = tiles.timeline(RUN)
    assert len(t) == 48
    assert t[0] == RUN - 69 * HOUR and t[-1] == RUN + 72 * HOUR
    assert RUN in t
    assert all(b - a == 3 * HOUR for a, b in zip(t, t[1:]))


def test_earlier_runs_and_steps():
    runs = tiles.earlier_runs(RUN)
    assert len(runs) == 12
    assert runs[0] == RUN - 72 * HOUR and runs[-1] == RUN - 6 * HOUR
    assert tiles.future_steps()[0] == 0 and tiles.future_steps()[-1] == 72 and len(tiles.future_steps()) == 25


def test_source_of_past_and_future():
    assert tiles.source_of(RUN, RUN + 3 * HOUR) == (RUN, 3)
    assert tiles.source_of(RUN, RUN + 72 * HOUR) == (RUN, 72)
    assert tiles.source_of(RUN, RUN) == (RUN - 6 * HOUR, 6)
    assert tiles.source_of(RUN, RUN - 3 * HOUR) == (RUN - 6 * HOUR, 3)
    assert tiles.source_of(RUN, RUN - 6 * HOUR) == (RUN - 12 * HOUR, 6)
    assert tiles.source_of(RUN, RUN - 69 * HOUR) == (RUN - 72 * HOUR, 3)
    # Every step of the timeline comes from a run and step that is downloaded.
    wanted = {(RUN, s) for s in tiles.future_steps()} | {(r, s) for r in tiles.earlier_runs(RUN) for s in tiles.EARLIER_STEPS}
    assert all(tiles.source_of(RUN, t) in wanted for t in tiles.timeline(RUN))


def fake_get(missing_runs=(), analysis_temp=None):
    """Raw fields of an invented atmosphere: 1 mm of rain per hour of forecast, 10 °C, dew point 0 °C."""

    def get(run, step, param):
        if run in missing_runs:
            return None
        if param == "tp":
            return 0.001 * step  # metres, accumulated: 3 mm per 3-hour step
        if param == "ssrd":
            return 1.8e6 * step  # J m-2, accumulated: 5.4 MJ per step
        if param == "2t":
            return analysis_temp if (analysis_temp is not None and step == 0) else 283.15
        if param == "2d":
            return 273.15
        if param == "10u":
            return 3.0
        if param == "10v":
            return 4.0
        if param == "tcc":
            return 0.4
        raise AssertionError(param)

    return get


def test_point_series_stitches_past_and_future():
    s = tiles.point_series(RUN, 38.7, -9.4, fake_get(), cloud_scale=100.0)
    assert set(s) == {"rain", "et0", "temp", "rh", "wind", "cloud"}
    assert all(len(v) == 48 for v in s.values())
    assert s["rain"] == [3] * 48  # every step, past and future, holds its own 3 mm
    assert s["temp"] == [10] * 48
    assert s["wind"] == [18] * 48
    assert s["cloud"] == [40] * 48
    assert s["rh"] == [50] * 48  # e°(0 °C) / e°(10 °C) = 0.6108 / 1.2279
    assert all(v is not None and v >= 0 for v in s["et0"])


def test_point_series_prefers_the_analysis_at_run_time():
    s = tiles.point_series(RUN, 38.7, -9.4, fake_get(analysis_temp=285.15), cloud_scale=100.0)
    at_run = tiles.timeline(RUN).index(RUN)
    assert s["temp"][at_run] == 12
    assert s["temp"][at_run - 1] == 10 and s["temp"][at_run + 1] == 10


def test_point_series_missing_earlier_run_gives_nulls():
    gone = RUN - 72 * HOUR
    s = tiles.point_series(RUN, 38.7, -9.4, fake_get(missing_runs={gone}), cloud_scale=100.0)
    for name in s:
        assert s[name][:2] == [None, None], name
        assert all(v is not None for v in s[name][2:]), name


def test_build_tiles_groups_by_degree():
    cells = [
        {"id": "geracoes", "lat": 38.6982, "lon": -9.3812},
        {"id": "g1287x1706", "lat": 38.75, "lon": -9.35},
        {"id": "sydney", "lat": -33.9, "lon": 151.2},
    ]
    series = [tiles.point_series(RUN, c["lat"], c["lon"], fake_get(), 100.0) for c in cells]
    out = tiles.build_tiles(cells, series, RUN, RUN + 8 * HOUR)
    assert set(out) == {"38_-10", "-34_151"}
    tile = out["38_-10"]
    assert tile["v"] == 1 and tile["model"] == "ifs025" and tile["run"] == RUN and tile["made"] == RUN + 8 * HOUR
    assert tile["t"] == tiles.timeline(RUN)
    assert set(tile["zones"]) == {"geracoes", "g1287x1706"}
    for zone in tile["zones"].values():
        assert set(zone) == {"rain", "et0", "temp", "rh", "wind", "cloud"}
        assert all(len(v) == len(tile["t"]) for v in zone.values())
    json.loads(tiles.encode(tile))  # valid JSON


def test_encode_refuses_nan():
    with pytest.raises(ValueError):
        tiles.encode({"x": float("nan")})


def test_batches_by_count():
    many = {f"{lat}_{lon}": {"v": 1} for lat in range(-10, 25) for lon in range(0, 20)}  # 700 tiles
    parts = list(tiles.batches(many, max_tiles=300, max_bytes=10**9))
    assert [len(p) for p in parts] == [300, 300, 100]
    assert sorted(k for p in parts for k in p) == sorted(many)


def test_batches_by_bytes():
    big = {f"{i}_0": {"pad": "x" * 1000} for i in range(10)}
    parts = list(tiles.batches(big, max_tiles=300, max_bytes=2500))
    assert [len(p) for p in parts] == [2] * 5
    # One tile above the limit still goes out, alone.
    assert [len(p) for p in tiles.batches(big, max_tiles=300, max_bytes=10)] == [1] * 10
    assert list(tiles.batches({})) == []
