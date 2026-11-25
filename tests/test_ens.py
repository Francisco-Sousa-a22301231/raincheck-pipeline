"""The ENS rain chance: where the members are, the chance, when a run waits for it, and how tiles carry it."""

import json

import pytest

from raincheck_pipeline import cli, ecmwf, ens, tiles
from raincheck_pipeline.http import HttpError
from raincheck_pipeline.tiles import HOUR

RUN = 1795564800  # 2026-11-25 00 UTC


def test_parse_members_keeps_tp_of_every_member_in_order():
    rows = [
        {"levtype": "sfc", "type": "pf", "param": "tp", "number": "2", "_offset": 200, "_length": 10},
        {"levtype": "sfc", "type": "pf", "param": "2t", "number": "1", "_offset": 0, "_length": 10},
        {"levtype": "sfc", "type": "pf", "param": "tp", "number": "1", "_offset": 100, "_length": 10},
        {"levtype": "pl", "type": "pf", "param": "tp", "number": "3", "_offset": 300, "_length": 10},
    ]
    fields = ens.parse_members("\n".join(json.dumps(r) for r in rows), "x.grib2", ("b",))
    assert [(f.param, f.offset) for f in fields] == [("tp#1", 100), ("tp#2", 200)]


def test_paths_and_steps():
    assert ens.ens_path(RUN, 72, "index") == "20261125/00z/ifs/0p25/enfo/20261125000000-72h-enfo-ef.index"
    assert ens.steps() == list(range(3, 75, 3))


def test_is_out_asks_every_source_with_head(monkeypatch):
    asked = []

    def request(url, method="GET", **kw):
        asked.append((url.split("/")[2], method))
        if "first" in url:
            raise HttpError(404, url)
        return 200, b""

    monkeypatch.setattr(ens, "request", request)
    assert ens.is_out(RUN, ["https://first", "https://second"]) is True
    assert asked == [("first", "HEAD"), ("second", "HEAD")]


def test_is_out_is_false_when_nobody_has_it_or_can_answer(monkeypatch):
    def request(url, method="GET", **kw):
        if "down" in url:
            raise OSError("no route")
        raise HttpError(503 if "busy" in url else 404, url)

    monkeypatch.setattr(ens, "request", request)
    assert ens.is_out(RUN, ["https://gone", "https://busy", "https://down"]) is False


def test_chance_from_accumulations():
    np = pytest.importorskip("numpy")
    m = ens.MIN_MEMBERS
    # m members, 2 points. Point 0: a quarter of the members hold 0.5 mm in step 3, nothing after.
    # Point 1: every member 0.2 mm per step (under the 0.3 mm bar), so always 0 %.
    a3 = np.zeros((m, 2), dtype=np.float32)
    a3[: m // 4, 0] = 0.0005
    a3[:, 1] = 0.0002
    a6 = a3.copy()
    a6[:, 1] += 0.0002
    p = ens.chance([a3, a6])
    assert p.shape == (2, 2)
    assert p[0].tolist() == [25.0, 0.0]
    assert p[1].tolist() == [0.0, 0.0]


def test_chance_needs_enough_members():
    np = pytest.importorskip("numpy")
    a = np.full((ens.MIN_MEMBERS, 1), np.nan, dtype=np.float32)
    a[: ens.MIN_MEMBERS - 1] = 0.001
    assert np.isnan(ens.chance([a])[0, 0])


def test_chance_on_timeline_only_ahead():
    p = tiles.chance_on_timeline(RUN, {s: (s % 100) * 1.0 for s in ens.steps()} | {9: float("nan"), 12: None})
    t = tiles.timeline(RUN)
    assert len(p) == len(t) == 48
    at_run = t.index(RUN)
    assert all(v is None for v in p[: at_run + 1])  # the past and the run's own time: no chance
    assert p[at_run + 1] == 3 and p[at_run + 2] == 6
    assert p[at_run + 3] is None and p[at_run + 4] is None  # NaN and missing
    assert p[-1] == 72


def fields_complete(*runs):
    full = {p: object() for p in ecmwf.PARAMS}
    return lambda run, step, sources=None, ask_all=True, params=ecmwf.PARAMS: full if run in runs else None


def test_newest_run_takes_a_run_with_its_ens(monkeypatch):
    monkeypatch.setattr(ecmwf, "step_fields", fields_complete(RUN))
    monkeypatch.setattr(ens, "is_out", lambda run: True)
    assert cli.newest_run(RUN + 8 * HOUR) == (RUN, True)


def test_newest_run_waits_for_the_ens_then_goes_without(monkeypatch):
    monkeypatch.setattr(ecmwf, "step_fields", fields_complete(RUN, RUN - 6 * HOUR))
    monkeypatch.setattr(ens, "is_out", lambda run: run != RUN)
    # 8 h after the run: HRES is out, the ENS not yet -> the run before (with its ENS).
    assert cli.newest_run(RUN + 8 * HOUR) == (RUN - 6 * HOUR, True)
    # 10 h after it: built without the chance.
    assert cli.newest_run(RUN + ens.WAIT_H * HOUR) == (RUN, False)
    # Asked to leave the ENS out: no wait.
    assert cli.newest_run(RUN + 8 * HOUR, with_ens=False) == (RUN, False)


def test_newest_run_none_when_nothing_is_complete(monkeypatch):
    monkeypatch.setattr(ecmwf, "step_fields", fields_complete())
    assert cli.newest_run(RUN) == (None, False)


def test_series_and_tiles_carry_p_only_with_a_chance():
    np = pytest.importorskip("numpy")
    cells = [{"id": "a", "lat": 38.7, "lon": -9.4}, {"id": "b", "lat": 38.8, "lon": -9.1}]
    where = {(c["lat"], c["lon"]): i for i, c in enumerate(cells)}
    chance = np.array([[10.0, 90.0]] * len(ens.steps()))
    with_p = cli.series_for(RUN, cells, {}, where, 1.0, chance)
    without = cli.series_for(RUN, cells, {}, where, 1.0)
    assert "p" not in without[0]
    assert with_p[0]["p"][-1] == 10 and with_p[1]["p"][-1] == 90
    tile = tiles.build_tiles(cells, with_p, RUN, RUN + 8 * HOUR)["38_-10"]
    assert all(len(z["p"]) == len(tile["t"]) for z in tile["zones"].values())
    json.loads(tiles.encode(tile))


def test_a_failed_ens_leaves_the_chance_out(monkeypatch, tmp_path):
    points = tmp_path / "points.json"
    points.write_text(json.dumps([{"id": "a", "lat": 38.7, "lon": -9.4}]))

    class FakeSampler:
        cloud_scale = 1.0
        grid = None

    monkeypatch.setattr(cli, "newest_run", lambda now, with_ens=True: (RUN, True))
    monkeypatch.setattr(cli, "extract", lambda run, points, threads: ({}, FakeSampler()))

    def broken(*a, **k):
        raise RuntimeError("ENS step 3: 0 members on the sources")

    monkeypatch.setattr(cli, "extract_ens", broken)
    assert cli.main(["--dry-run", "--points-file", str(points), "--out", str(tmp_path / "out")]) == 0
    tile = json.loads((tmp_path / "out" / "38_-10.json").read_text())
    assert "p" not in tile["zones"]["a"]
