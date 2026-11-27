"""The four rain sources: where their files are, how their inventories are read, and when a run is out."""

import pytest

from raincheck_pipeline import aifs, ecmwf, gefs, gfs, icon_eu, noaa
from raincheck_pipeline.http import HttpError

RUN = 1795737600  # 2026-11-27 00 UTC

# Lines as NOAA writes them (GFS f009 and f024, GEFS f006, GFS f003 with its two identical APCP records).
GFS_F009 = """595:429000000:d=2026112700:PRATE:surface:6-9 hour ave fcst:
596:430066331:d=2026112700:APCP:surface:6-9 hour acc fcst:
597:430367565:d=2026112700:APCP:surface:0-9 hour acc fcst:
598:430759865:d=2026112700:ACPCP:surface:6-9 hour acc fcst:
"""
GFS_F024 = """596:426599022:d=2026112700:APCP:surface:18-24 hour acc fcst:
597:426951187:d=2026112700:APCP:surface:0-1 day acc fcst:
598:427445533:d=2026112700:ACPCP:surface:18-24 hour acc fcst:
"""
GFS_F072 = """596:430140000:d=2026112700:APCP:surface:66-72 hour acc fcst:
597:430490181:d=2026112700:APCP:surface:0-3 day acc fcst:
"""
GFS_F003 = """596:424951560:d=2026112700:APCP:surface:0-3 hour acc fcst:
597:425255137:d=2026112700:APCP:surface:0-3 hour acc fcst:
598:425558714:d=2026112700:ACPCP:surface:0-3 hour acc fcst:
"""
GEFS_F006 = """17:9000000:d=2026112700:TMP:2 m above ground:6 hour fcst:ENS=+30
18:9554106:d=2026112700:APCP:surface:0-6 hour acc fcst:ENS=+30
19:10058587:d=2026112700:CSNOW:surface:0-6 hour ave fcst:ENS=+30
"""


def test_parse_idx_lengths_and_the_last_record():
    records = noaa.parse_idx(GFS_F009)
    assert [r.number for r in records] == [595, 596, 597, 598]
    assert records[1].length == 430367565 - 430066331
    assert records[-1].length is None  # to the end of the file
    assert records[2].when == "0-9 hour acc fcst" and records[2].acc_hours() == (0, 9)
    assert records[0].acc_hours() is None  # an average, not an accumulation


def test_parse_idx_counts_fields_sharing_a_message_once():
    text = "10:100:d=x:TMP:surface:anl:\n11.1:200:d=x:UGRD:10 m:anl:\n11.2:200:d=x:VGRD:10 m:anl:\n12:350:d=x:LAND:surface:anl:\n"
    records = noaa.parse_idx(text)
    assert [(r.number, r.var, r.length) for r in records] == [(10, "TMP", 100), (11, "UGRD", 150), (11, "VGRD", 150), (12, "LAND", None)]
    assert noaa.parse_idx("garbage\n\n") == []


def test_day_labels():
    assert noaa.parse_idx(GFS_F024)[1].acc_hours() == (0, 24)
    assert noaa.parse_idx(GFS_F072)[1].acc_hours() == (0, 72)


def test_gfs_takes_the_run_total():
    rec, start = noaa.rain_record(noaa.parse_idx(GFS_F009), 9, run_total=True)
    assert (rec.number, start, rec.length) == (597, 0, 430759865 - 430367565)
    rec, start = noaa.rain_record(noaa.parse_idx(GFS_F024), 24, run_total=True)
    assert (rec.number, start) == (597, 0)
    rec, start = noaa.rain_record(noaa.parse_idx(GFS_F072), 72, run_total=True)
    assert (rec.number, start) == (597, 0)
    # f003: both records are "0-3 hour acc" (the same numbers): the first one.
    rec, start = noaa.rain_record(noaa.parse_idx(GFS_F003), 3, run_total=True)
    assert (rec.number, start) == (596, 0)
    # Not the step asked for, or no rain at all: nothing.
    assert noaa.rain_record(noaa.parse_idx(GFS_F009), 12, run_total=True) is None
    assert noaa.rain_record(noaa.parse_idx("1:0:d=x:TMP:surface:anl:\n"), 0, run_total=True) is None


def test_gefs_takes_the_bucket():
    rec, start = noaa.rain_record(noaa.parse_idx(GEFS_F006), 6, run_total=False)
    assert (rec.number, start, rec.length) == (18, 0, 10058587 - 9554106)
    rec, start = noaa.rain_record(noaa.parse_idx(GFS_F009), 9, run_total=False)
    assert (rec.number, start) == (596, 6)  # the 6-9 h bucket


def test_noaa_download_checks_the_answer(monkeypatch):
    rec = noaa.Record(5, 100, 8, "APCP", "surface", "0-3 hour acc fcst")
    seen = {}

    def request(url, headers=None, **kw):
        seen["range"] = headers["Range"]
        return 206, b"GRIB7777"

    monkeypatch.setattr(noaa, "request", request)
    assert noaa.download("https://x/f", rec) == b"GRIB7777"
    assert seen["range"] == "bytes=100-107"
    assert noaa.download("https://x/f", noaa.Record(5, 100, None, "APCP", "surface", "")) == b"GRIB7777"
    assert seen["range"] == "bytes=100-"
    monkeypatch.setattr(noaa, "request", lambda url, **kw: (200, b"<html>"))
    with pytest.raises(ValueError):
        noaa.download("https://x/f", rec)


def test_read_idx_and_exists_tell_missing_from_trouble(monkeypatch):
    def request(url, method="GET", **kw):
        if "gone" in url:
            raise HttpError(404, url)
        if "hidden" in url:
            raise HttpError(403, url)  # S3: a key that does not exist in a bucket that cannot be listed
        if "busy" in url:
            raise HttpError(503, url)
        return 200, GFS_F009.encode()

    monkeypatch.setattr(noaa, "request", request)
    assert noaa.read_idx("https://gone/x.idx") is None
    assert noaa.read_idx("https://hidden/x.idx") is None
    assert len(noaa.read_idx("https://ok/x.idx")) == 4
    assert noaa.exists("https://ok/x") and not noaa.exists("https://gone/x")
    with pytest.raises(HttpError):
        noaa.exists("https://busy/x")


def test_paths():
    assert gfs.file_url(RUN, 9) == "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261127/00/atmos/gfs.t00z.pgrb2.0p25.f009"
    assert gefs.file_url(RUN + 6 * 3600, 72, 0).endswith("gefs.20261127/06/atmos/pgrb2sp25/gec00.t06z.pgrb2s.0p25.f072")
    assert gefs.file_url(RUN, 3, 30).endswith("/gep30.t00z.pgrb2s.0p25.f003")
    assert icon_eu.file_url(RUN + 12 * 3600, 75) == (
        "https://opendata.dwd.de/weather/nwp/icon-eu/grib/12/tot_prec/"
        "icon-eu_europe_regular-lat-lon_single-level_2026112712_075_TOT_PREC.grib2.bz2")
    assert icon_eu.land_url(RUN).endswith("/00/fr_land/icon-eu_europe_regular-lat-lon_time-invariant_2026112700_FR_LAND.grib2.bz2")
    assert ecmwf.file_path(RUN, 72, "oper", "index", aifs.FOLDER) == "20261127/00z/aifs-single/0p25/oper/20261127000000-72h-oper-fc.index"
    assert ecmwf.file_path(RUN, 72, "oper", "index") == "20261127/00z/ifs/0p25/oper/20261127000000-72h-oper-fc.index"
    assert len(gefs.MEMBERS) == 31 and gefs.member_name(0) == "gec00" and gefs.member_name(7) == "gep07"


def test_runs_newest_first_every_six_hours():
    now = RUN + 10 * 3600 + 17 * 60
    for mod in (gfs, gefs, icon_eu, aifs):
        assert mod.runs(now) == [RUN + 6 * 3600, RUN, RUN - 6 * 3600, RUN - 12 * 3600]


def test_gfs_fetch_reads_the_inventory_then_one_range(monkeypatch):
    asked = []

    def request(url, headers=None, method="GET", **kw):
        asked.append((url.rsplit("/", 1)[1], (headers or {}).get("Range")))
        if url.endswith(".idx"):
            return 200, GFS_F009.encode()
        return 206, b"GRIB" + b"\0" * (430759865 - 430367565 - 8) + b"7777"

    monkeypatch.setattr(noaa, "request", request)
    start, message = gfs.fetch(RUN, 9)
    assert start == 0 and message[:4] == b"GRIB"
    assert asked == [("gfs.t00z.pgrb2.0p25.f009.idx", None), ("gfs.t00z.pgrb2.0p25.f009", "bytes=430367565-430759864")]


def test_gefs_is_out_asks_the_control_and_the_last_member(monkeypatch):
    asked = []

    def request(url, method="GET", **kw):
        asked.append((url.rsplit("/", 1)[1], method))
        if "gep30" in url:
            raise HttpError(404, url)
        return 200, b""

    monkeypatch.setattr(noaa, "request", request)
    assert gefs.is_out(RUN, [72, 75]) is False
    assert asked == [("gec00.t00z.pgrb2s.0p25.f075.idx", "HEAD"), ("gec00.t00z.pgrb2s.0p25.f072.idx", "HEAD"),
                     ("gep30.t00z.pgrb2s.0p25.f075.idx", "HEAD")]  # the last step first, then the rest
    monkeypatch.setattr(noaa, "request", lambda url, **kw: (_ for _ in ()).throw(HttpError(500, url)))
    with pytest.raises(ecmwf.SourceTrouble):
        gefs.is_out(RUN, [72, 75])


def test_icon_is_out_and_bz2_files(monkeypatch):
    import bz2

    def request(url, method="GET", **kw):
        if "_075_" in url:
            raise HttpError(404, url)
        if "_078_" in url:
            raise OSError("no route")
        return 200, bz2.compress(b"GRIB....7777")

    monkeypatch.setattr(icon_eu, "request", request)
    assert icon_eu.is_out(RUN, [3, 69, 72]) is True
    assert icon_eu.is_out(RUN, [72, 75]) is False
    with pytest.raises(ecmwf.SourceTrouble):
        icon_eu.is_out(RUN, [78])
    assert icon_eu.fetch(RUN, 9) == (0, b"GRIB....7777")
    assert icon_eu.fetch(RUN, 75) is None


def test_aifs_asks_the_aifs_folder_in_oper_only(monkeypatch):
    asked = []

    def request(url, **kw):
        asked.append(url)
        if "-78h-" in url:
            raise HttpError(404, url)
        return 200, b'{"levtype": "sfc", "type": "fc", "param": "tp", "_offset": 10, "_length": 20}\n'

    monkeypatch.setattr(ecmwf, "request", request)
    assert aifs.is_out(RUN, [66, 72]) is True
    assert asked[-1].endswith("/20261127/00z/aifs-single/0p25/oper/20261127000000-72h-oper-fc.index")
    asked.clear()
    assert aifs.is_out(RUN, [72, 78]) is False
    assert len(asked) == 1  # the first "not there" settles it, and there is no `scda` to try


def test_gfs_is_out_only_with_every_step(monkeypatch):
    # 27/11/2026, GFS 06 UTC at run + 4 h: f075 was out, f060 and f063 not yet.
    missing = {"f060", "f063"}

    def request(url, method="GET", **kw):
        if url.split(".")[-2] in missing:
            raise HttpError(404, url)
        return 200, b""

    monkeypatch.setattr(noaa, "request", request)
    steps = list(range(57, 78, 3))
    assert gfs.is_out(RUN, steps) is False
    missing.clear()
    assert gfs.is_out(RUN, steps) is True
    missing.add("f075")
    assert gfs.is_out(RUN, [72, 75]) is False
    assert gfs.is_out(RUN, []) is False
