from raincheck_pipeline import ecmwf, targets
from raincheck_pipeline.tiles import PARAMS


def test_targets_from_env():
    env = {
        "TARGETS": "staging, live,local,plain,empty",
        "STAGING_URL": "https://staging.example.org/",
        "STAGING_TOKEN": "s3cret",
        "LIVE_URL": "https://example.org",  # no token
        "LOCAL_URL": "http://127.0.0.1:8787",
        "LOCAL_TOKEN": "dev",
        "PLAIN_URL": "http://example.org",
        "PLAIN_TOKEN": "x",
    }
    usable, notes = targets.from_env(env)
    assert [(t.name, t.url) for t in usable] == [("STAGING", "https://staging.example.org"), ("LOCAL", "http://127.0.0.1:8787")]
    assert any("LIVE_TOKEN is not set" in n for n in notes)
    assert any("PLAIN_URL must be https" in n for n in notes)
    assert any("EMPTY_URL is not set" in n for n in notes)
    assert not any("s3cret" in n for n in notes)
    assert targets.from_env({}) == ([], [])


def test_valid_cells():
    cells, dropped = targets.valid_cells({"cells": [
        {"id": "geracoes", "lat": 38.6982, "lon": -9.3812},
        {"id": "g1286x1706", "lat": "38.65", "lon": -9.35},
        {"id": "geracoes", "lat": 1, "lon": 1},  # duplicate id
        {"id": "Bad Id", "lat": 1, "lon": 1},
        {"id": "far", "lat": 91, "lon": 0},
        {"lat": 1, "lon": 1},
    ]})
    assert [c["id"] for c in cells] == ["geracoes", "g1286x1706"]
    assert cells[1]["lat"] == 38.65
    assert dropped == 4


def test_cells_signature_ignores_order():
    a = [{"id": "a", "lat": 1.0, "lon": 2.0}, {"id": "b", "lat": 3.0, "lon": 4.0}]
    assert targets.cells_signature(a) == targets.cells_signature(a[::-1])
    assert targets.cells_signature(a) != targets.cells_signature(a[:1])
    assert targets.cells_signature(a) != targets.cells_signature([{**a[0], "lat": 1.1}, a[1]])


def test_needs():
    assert targets.needs({"run": None}, 100, "x")
    assert targets.needs({}, 100, "x")
    assert targets.needs({"run": 90, "cells": "x"}, 100, "x")
    assert not targets.needs({"run": 100, "cells": "x"}, 100, "x")
    assert targets.needs({"run": 100, "cells": "y"}, 100, "x")  # the list of points changed
    assert not targets.needs({"run": 110, "cells": "y"}, 100, "x")  # never back to an older run


def test_parse_index_and_paths():
    run = 1795392000  # 2026-11-23 00 UTC
    path = ecmwf.file_path(run, 6, "oper", "grib2")
    assert path == "20261123/00z/ifs/0p25/oper/20261123000000-6h-oper-fc.grib2"
    text = "\n".join([
        '{"type": "fc", "stream": "oper", "step": "6", "levtype": "sfc", "param": "2t", "_offset": 100, "_length": 50}',
        '{"type": "fc", "stream": "oper", "step": "6", "levtype": "pl", "levelist": "500", "param": "t", "_offset": 150, "_length": 10}',
        '{"type": "fc", "stream": "oper", "step": "6", "levtype": "sfc", "param": "tp", "_offset": 160, "_length": 20}',
        "",
    ])
    fields = ecmwf.parse_index(text, path, ("https://a", "https://b"))
    assert set(fields) == {"2t", "tp"}
    assert (fields["2t"].offset, fields["2t"].length, fields["2t"].path) == (100, 50, path)
    assert set(PARAMS) == {"tp", "2t", "2d", "10u", "10v", "tcc", "ssrd"}
