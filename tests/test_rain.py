"""The rain-model job: the common timeline, accumulations to 3-hour amounts, GEFS, tiles, upload, the check."""

import json
import math

import pytest

from raincheck_pipeline import rain, rainmath, targets
from raincheck_pipeline.rainmath import HOUR

np = pytest.importorskip("numpy")

RUN = 1795737600  # 2026-11-27 00 UTC
KEY_RE = r"^-?\d{1,2}_-?\d{1,3}$"


# ---------------------------------------------------------------- timeline


def test_timeline_starts_with_the_step_made_falls_in():
    made = RUN + 10 * HOUR + 17 * 60  # 10:17 UTC
    t = rainmath.timeline(made)
    assert len(t) == 24
    assert t[0] == RUN + 12 * HOUR and t[-1] == RUN + 81 * HOUR
    assert all(b - a == 3 * HOUR for a, b in zip(t, t[1:]))
    # Right on a step's end: that step is over, the next one is the first.
    assert rainmath.timeline(RUN + 9 * HOUR)[0] == RUN + 12 * HOUR


def test_models_with_different_runs_share_the_same_steps():
    t = rainmath.timeline(RUN + 10 * HOUR)
    assert rainmath.leads(RUN, t)[:3] == [12, 15, 18]  # GEFS of 00 UTC
    assert rainmath.leads(RUN + 6 * HOUR, t)[:3] == [6, 9, 12]  # ICON-EU of 06 UTC
    assert rainmath.leads(RUN - 6 * HOUR, t)[-1] == 87  # AIFS of 18 UTC the day before


def test_needed_steps():
    leads = [6, 9, 12, 15]
    assert rainmath.needed_steps("totals", leads, 120) == [3, 6, 9, 12, 15]
    assert rainmath.needed_steps("totals", [3, 6], 120) == [3, 6]  # step 0 is never fetched
    # GEFS: a bucket at 9 or 15 h is already the step; at 6 and 12 the step before comes off.
    assert rainmath.needed_steps("buckets", leads, 240) == [3, 6, 9, 12, 15]
    assert rainmath.needed_steps("buckets", [9, 12, 15], 240) == [9, 12, 15]
    # AIFS: the 6-hour windows around each step.
    assert rainmath.needed_steps("six_hourly", [3], 360) == [6]
    assert rainmath.needed_steps("six_hourly", [9, 12, 15], 360) == [6, 12, 18]
    # Past the model's last step: left out (those steps stay null).
    assert rainmath.needed_steps("totals", [117, 120, 123], 120) == [114, 117, 120]
    assert rainmath.needed_steps("six_hourly", [354, 357], 354) == [348, 354]
    with pytest.raises(ValueError):
        rainmath.needed_steps("hourly", leads, 1)


# ---------------------------------------------------------------- accumulations


def arr(*v):
    return np.array(v, dtype=np.float32)


def test_totals_since_the_start_gfs_and_icon():
    # Two places. Totals: 1 mm by 3 h, 1 mm by 6 h (nothing in 3-6, packing gives -0.02), 4 mm by 9 h.
    acc = {3: (0, arr(1.0, 0.0)), 6: (0, arr(0.98, 0.5)), 9: (0, arr(4.0, 0.5))}
    out = rainmath.three_hourly([3, 6, 9, 12], acc.get, 120)
    assert out[0].tolist() == [1.0, 0.0]
    assert out[1].tolist() == [0.0, 0.5]  # -0.02 clipped
    assert np.allclose(out[2], [3.02, 0.0])
    assert out[3] is None  # step 12 not read


def test_gefs_buckets_with_negative_clipping():
    # 6-hour buckets: 3 h holds 0-3, 6 h holds 0-6, 9 h holds 6-9, 12 h holds 6-12.
    acc = {
        3: (0, arr(0.4, 0.0, np.nan)),
        6: (0, arr(0.4, -0.09, 1.0)),  # place 2: the 0-6 bucket a little below its 0-3 one
        9: (6, arr(2.0, 0.0, 1.0)),
        12: (6, arr(2.5, 0.0, 1.0)),
    }
    out = rainmath.three_hourly([3, 6, 9, 12], acc.get, 240)
    assert out[0].tolist()[:2] == [pytest.approx(0.4), 0.0] and math.isnan(out[0][2])
    assert out[1].tolist()[:2] == [0.0, 0.0] and math.isnan(out[1][2])  # NaN stays "not known"
    assert out[2].tolist() == [2.0, 0.0, 1.0]  # a bucket that starts at the step's start is the step
    assert out[3].tolist() == [pytest.approx(0.5), 0.0, 0.0]
    # The step before is missing, or belongs to another bucket: no amount.
    assert rainmath.three_hourly([12], {12: (6, arr(1.0))}.get, 240) == [None]
    assert rainmath.three_hourly([12], {12: (6, arr(1.0)), 9: (0, arr(0.5))}.get, 240) == [None]


def test_aifs_six_hour_amounts_are_split_over_two_steps():
    acc = {6: (0, arr(2.0)), 12: (0, arr(2.0)), 18: (0, arr(7.0))}
    out = rainmath.six_hourly_halves([3, 6, 9, 12, 15, 18, 21], acc.get, 360)
    assert [None if o is None else float(o[0]) for o in out] == [1.0, 1.0, 0.0, 0.0, 2.5, 2.5, None]
    # Packing noise below zero is dry; past the last step nothing.
    acc = {6: (0, arr(2.0)), 12: (0, arr(1.99))}
    assert [float(o[0]) for o in rainmath.six_hourly_halves([9, 12], acc.get, 360)] == [0.0, 0.0]
    assert rainmath.six_hourly_halves([357], acc.get, 354) == [None]


# ---------------------------------------------------------------- GEFS


def test_gefs_chance_and_median():
    # 31 members, 2 places. Place 0: 10 members with 0.5 mm, 21 with 0.1 mm. Place 1: everyone 2 mm.
    rows = [[arr(0.5 if m < 10 else 0.1, 2.0)] for m in range(31)]
    chance, median = rainmath.ensemble(rows, 2)
    assert chance[0, 0] == pytest.approx(100 * 10 / 31) and chance[0, 1] == 100
    assert median[0, 0] == pytest.approx(0.1) and median[0, 1] == pytest.approx(2.0)


def test_gefs_with_missing_members():
    # Step 0: 24 members read (one fewer than needed): no chance, no median. Step 1: 26 read, but at
    # place 1 three of them are NaN (23 known there): that place has none.
    rows = []
    for m in range(31):
        s0 = arr(1.0, 1.0) if m < 24 else None
        s1 = (arr(1.0, np.nan if m < 3 else 0.0) if m < 26 else None)
        rows.append([s0, s1])
    chance, median = rainmath.ensemble(rows, 2)
    assert np.isnan(chance[0]).all() and np.isnan(median[0]).all()
    assert chance[1, 0] == 100 and median[1, 0] == 1.0
    assert np.isnan(chance[1, 1]) and np.isnan(median[1, 1])


def test_rounding_as_the_tile_writes_it():
    assert rainmath.mm(0.04) == 0 and rainmath.mm(0.05) in (0, 0.1)
    assert rainmath.mm(1.26) == 1.3 and rainmath.mm(2.0) == 2 and isinstance(rainmath.mm(2.0), int)
    assert rainmath.mm(-0.09) == 0
    assert rainmath.mm(float("nan")) is None and rainmath.mm(None) is None
    assert rainmath.percent(32.258) == 32 and rainmath.percent(100.4) == 100 and rainmath.percent(np.nan) is None


# ---------------------------------------------------------------- tiles


def results_for(times):
    n = len(times)
    nan = float("nan")
    return {
        # Point 0 (Cascais), point 1 (Venice Beach: outside ICON-EU).
        "gfs": {"run": RUN, "inside": None, "series": {"gfs": [[0.04, 1.26]] + [[0.0, 0.0]] * (n - 2) + [None]}},
        "icon_eu": {"run": RUN + 6 * HOUR, "inside": [True, False], "series": {"icon_eu": [[2.0, nan]] * n}},
        "aifs": {"run": RUN - 6 * HOUR, "inside": None, "series": {"aifs": [[nan, 0.25]] * n}},
        "gefs": {"run": RUN, "members": 31, "inside": None,
                 "series": {"gefs_p": [[32.258, nan]] * n, "gefs_mm": [[0.15, nan]] * n}},
    }


def test_tiles_to_the_contract():
    made = RUN + 10 * HOUR
    times = rainmath.timeline(made)
    cells = [{"id": "cascais", "lat": 38.70, "lon": -9.38}, {"id": "venice-beach", "lat": 33.985, "lon": -118.4695}]
    where = {(c["lat"], c["lon"]): i for i, c in enumerate(cells)}
    tiles = rain.build_rain_tiles(cells, where, made, times, results_for(times))
    assert set(tiles) == {"38_-10", "33_-119"}
    tile = json.loads(rain.encode(tiles["38_-10"]))  # encodes: no NaN anywhere
    assert set(tile) == {"v", "made", "t", "models", "zones"}
    assert tile["v"] == 1 and tile["made"] == made and tile["t"] == times
    assert tile["models"] == {"gfs": {"run": RUN}, "gefs": {"run": RUN, "members": 31},
                              "icon_eu": {"run": RUN + 6 * HOUR}, "aifs": {"run": RUN - 6 * HOUR}}
    zone = tile["zones"]["cascais"]
    assert list(zone) == ["gfs", "icon_eu", "aifs", "gefs_p", "gefs_mm"]
    assert all(len(v) == 24 for v in zone.values())
    assert zone["gfs"][0] == 0 and zone["gfs"][-1] is None
    assert zone["icon_eu"][0] == 2
    assert zone["aifs"][0] is None
    assert zone["gefs_p"][0] == 32 and zone["gefs_mm"][0] in (0.1, 0.2)
    far = json.loads(rain.encode(tiles["33_-119"]))["zones"]["venice-beach"]
    assert "icon_eu" not in far  # outside the domain: no key at all
    assert far["gfs"][0] == 1.3 and far["gefs_p"][0] is None and far["aifs"][0] in (0.2, 0.3)
    import re

    assert all(re.match(KEY_RE, k) for k in tiles)


def test_a_model_left_out_is_absent_everywhere():
    made = RUN + 10 * HOUR
    times = rainmath.timeline(made)
    res = results_for(times)
    del res["gefs"]
    cells = [{"id": "cascais", "lat": 38.70, "lon": -9.38}]
    tile = rain.build_rain_tiles(cells, {(38.70, -9.38): 0}, made, times, res)["38_-10"]
    assert "gefs" not in tile["models"]
    assert not any(k.startswith("gefs") for k in tile["zones"]["cascais"])


# ---------------------------------------------------------------- upload


def test_rain_upload_in_batches_with_last_at_the_end(monkeypatch):
    sent = []

    def request(url, method="GET", data=None, headers=None, **kw):
        sent.append((url, method, json.loads(data), headers["Authorization"]))
        return 200, b'{"ok":true}'

    monkeypatch.setattr(targets, "request", request)
    made = RUN + 10 * HOUR
    times = rainmath.timeline(made)
    cells = [{"id": f"z{i}", "lat": 30.5 + i // 20, "lon": -9.5 + i % 20} for i in range(130)]
    where = {(c["lat"], c["lon"]): i for i, c in enumerate(cells)}
    n = len(times)
    res = {"gfs": {"run": RUN, "inside": None, "series": {"gfs": [[1.0] * 130] * n}}}
    tiles = rain.build_rain_tiles(cells, where, made, times, res)
    assert len(tiles) == 130
    target = targets.Target("SITE", "https://site.example", "tok")
    count = targets.upload_rain(target, tiles, "abc", {"gfs": RUN}, max_tiles=60, max_bytes=128 * 1024)
    assert count == 3 and len(sent) == 3
    assert [len(b["tiles"]) for _, _, b, _ in sent] == [60, 60, 10]
    assert [b["last"] for _, _, b, _ in sent] == [False, False, True]
    assert all(u == "https://site.example/api/admin/rain" and m == "PUT" and a == "Bearer tok" for u, m, _, a in sent)
    assert sent[-1][2]["models"] == {"gfs": RUN} and sent[-1][2]["cells"] == "abc"
    assert all(len(rain.encode(b)) <= 128 * 1024 + 2048 for _, _, b, _ in sent)
    # Nothing to send still closes the upload.
    sent.clear()
    assert targets.upload_rain(target, {}, "abc", {}, 60, 128 * 1024) == 1 and sent[0][2]["last"] is True


# ---------------------------------------------------------------- which targets need a build


def test_newer_models():
    found = {"gfs": RUN + 6 * HOUR, "gefs": RUN, "icon_eu": RUN + 6 * HOUR}
    assert rain.newer_models({}, found) == ["gfs", "gefs", "icon_eu"]
    assert rain.newer_models({"run": None}, found) == ["gfs", "gefs", "icon_eu"]
    status = {"models": {"gfs": RUN, "gefs": RUN, "icon_eu": RUN + 12 * HOUR, "aifs": RUN}}
    assert rain.newer_models(status, found) == ["gfs"]  # ICON-EU held newer: never back; AIFS not found here
    assert rain.newer_models({"models": {"gfs": True, "gefs": "x"}}, {"gfs": RUN, "gefs": RUN}) == ["gfs", "gefs"]


ENV = {"TARGETS": "SITE", "SITE_URL": "https://site.example", "SITE_TOKEN": "t"}
FOUND = {"gfs": RUN, "gefs": RUN, "icon_eu": RUN, "aifs": RUN}


@pytest.fixture
def asked(monkeypatch):
    class Calls(list):
        state: dict

    calls = Calls()
    cells = [{"id": "a", "lat": 1.0, "lon": 2.0}]
    state = {"status": {"models": dict(FOUND), "cells": targets.cells_signature(cells)}, "cells": cells}

    def get_status(target, path=targets.BASE_STATUS):
        calls.append(("status", path))
        return dict(state["status"])

    def get_cells(target):
        calls.append("cells")
        return list(state["cells"])

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(rain, "newest_runs", lambda now, times, models=rain.MODELS: dict(FOUND))
    monkeypatch.setattr(targets, "get_status", get_status)
    monkeypatch.setattr(targets, "get_cells", get_cells)
    monkeypatch.setattr(rain, "extract_all", lambda *a, **k: pytest.fail("nothing may be downloaded here"))
    calls.state = state
    return calls


def output(tmp_path, monkeypatch):
    path = tmp_path / "out"
    path.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return path


def test_check_up_to_date_asks_only_the_rain_status(asked, tmp_path, monkeypatch):
    out = output(tmp_path, monkeypatch)
    assert rain.main(["--check"]) == 0
    assert asked == [("status", "/api/admin/rain/status")]
    assert out.read_text() == "needed=false\n"


def test_check_with_a_newer_model_run(asked, tmp_path, monkeypatch):
    asked.state["status"]["models"]["icon_eu"] = RUN - 6 * HOUR
    out = output(tmp_path, monkeypatch)
    assert rain.main(["--check"]) == 0
    assert asked == [("status", "/api/admin/rain/status")]  # the places are not read for a check
    assert out.read_text() == "needed=true\n"


def test_check_with_cells_compares_the_places(asked, tmp_path, monkeypatch):
    out = output(tmp_path, monkeypatch)
    assert rain.main(["--check", "--cells"]) == 0
    assert asked[-1] == "cells" and out.read_text() == "needed=false\n"
    asked.state["cells"].append({"id": "b", "lat": 3.0, "lon": 4.0})
    out.write_text("")
    assert rain.main(["--check", "--cells"]) == 0
    assert out.read_text() == "needed=true\n"


def test_through_the_cli_entry(asked):
    from raincheck_pipeline import cli

    assert cli.main(["rain"]) == 0  # up to date: nothing downloaded
    assert asked == [("status", "/api/admin/rain/status")]


def test_a_build_uploads_what_could_be_read(asked, monkeypatch):
    asked.state["status"] = {"run": None}
    times_seen = {}

    def extract_all(found, times, points, threads):
        times_seen["t"] = times
        n = len(times)
        # GEFS failed: only GFS comes back.
        return {"gfs": {"run": found["gfs"], "inside": None, "series": {"gfs": [[0.5] * len(points)] * n}}}

    uploads = []
    monkeypatch.setattr(rain, "extract_all", extract_all)
    monkeypatch.setattr(targets, "upload_rain", lambda target, tiles, sig, models, mt, mb: uploads.append((tiles, models)) or 1)
    assert rain.main([]) == 0
    tiles, models = uploads[0]
    assert models == {"gfs": RUN}
    tile = tiles["1_2"]
    assert tile["models"] == {"gfs": {"run": RUN}} and tile["zones"]["a"] == {"gfs": [0.5] * 24}


def test_nothing_read_uploads_nothing(asked, monkeypatch):
    asked.state["status"] = {"run": None}
    monkeypatch.setattr(rain, "extract_all", lambda *a, **k: {})
    monkeypatch.setattr(targets, "upload_rain", lambda *a, **k: pytest.fail("no upload without any model"))
    assert rain.main([]) == 1


# ---------------------------------------------------------------- extract_model with a fake decoder


class FakeSampler:
    """Stands in for grib.Sampler: a "message" is b"GRIB" + JSON of the values at the points."""

    def __init__(self, points):
        self.grid = None
        self.units = {}
        self.size = len(points)

    def set_land_mask(self, message):
        pass

    def sample(self, message):
        self.units["tp"] = "kg m**-2"
        return "tp", np.array(json.loads(message[4:]), dtype=np.float32)


class FakeModel:
    NAME = "gefs"
    KIND = "buckets"
    MAX_STEP_H = 240
    MEMBERS = list(range(31))

    def __init__(self, broken=(), missing_step=None):
        self.broken = set(broken)
        self.missing_step = missing_step

    def land(self, run):
        return None

    def fetch(self, run, step, member):
        if member in self.broken:
            raise OSError("reset")
        if step == self.missing_step:
            return None
        start = step - 3 if step % 6 == 3 else step - 6
        # Every member: 0.25 mm an hour at place 0; at place 1 only members 20 and up, 1 mm an hour.
        per_h = [0.25, 1.0 if (member or 0) >= 20 else 0.0]
        return start, b"GRIB" + json.dumps([v * (step - start) for v in per_h]).encode()


def test_extract_gefs_leaves_broken_members_out(monkeypatch):
    from raincheck_pipeline import grib

    monkeypatch.setattr(grib, "Sampler", FakeSampler)
    made = RUN + 10 * HOUR
    times = rainmath.timeline(made)
    out = rain.extract_model(FakeModel(broken={3, 4}), RUN, times, [(1.0, 2.0), (3.0, 4.0)], threads=4)
    assert out["members"] == 29
    p, med = out["series"]["gefs_p"], out["series"]["gefs_mm"]
    assert len(p) == 24
    assert p[0][0] == 100 and med[0][0] == pytest.approx(0.75)
    assert p[0][1] == pytest.approx(100 * 11 / 29) and med[0][1] == 0.0
    with pytest.raises(RuntimeError):
        rain.extract_model(FakeModel(broken=set(range(7))), RUN, times, [(1.0, 2.0)], threads=4)


def test_extract_deterministic_with_a_hole_fails(monkeypatch):
    from raincheck_pipeline import grib

    class Det(FakeModel):
        NAME = "gfs"
        KIND = "totals"
        MEMBERS = None

        def fetch(self, run, step, member):
            if step == self.missing_step:
                return None
            return 0, b"GRIB" + json.dumps([0.5 * step]).encode()

    monkeypatch.setattr(grib, "Sampler", FakeSampler)
    times = rainmath.timeline(RUN + 10 * HOUR)
    out = rain.extract_model(Det(), RUN, times, [(1.0, 2.0)], threads=2)
    assert [float(s[0]) for s in out["series"]["gfs"][:3]] == [1.5, 1.5, 1.5]
    with pytest.raises(RuntimeError):
        rain.extract_model(Det(missing_step=30), RUN, times, [(1.0, 2.0)], threads=2)
