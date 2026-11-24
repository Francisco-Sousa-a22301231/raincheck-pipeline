import pytest

from raincheck_pipeline import ecmwf
from raincheck_pipeline.http import HttpError
from raincheck_pipeline.tiles import PARAMS

RUN = 1795392000  # 2026-11-23 00 UTC
INDEX = "\n".join(
    '{"type": "fc", "levtype": "sfc", "param": "%s", "_offset": %d, "_length": 10}' % (p, 100 * i) for i, p in enumerate(PARAMS)
).encode()


def sources(monkeypatch, answers):
    """Replaces the HTTP call: `answers[host]` is bytes (the index), an HTTP status or an exception."""
    asked = []

    def request(url, **kwargs):
        host = url.split("/")[2]
        asked.append(url)
        answer = answers[host]
        if isinstance(answer, bytes):
            return 200, answer
        if isinstance(answer, int):
            raise HttpError(answer, url)
        raise answer

    monkeypatch.setattr(ecmwf, "request", request)
    return asked


BASES = ["https://portal", "https://mirror"]


def test_step_from_the_first_source_that_has_it(monkeypatch):
    sources(monkeypatch, {"portal": 404, "mirror": INDEX})
    fields = ecmwf.step_fields(RUN, 3, BASES)
    assert set(fields) == set(PARAMS)
    assert fields["tp"].bases == ("https://mirror", "https://portal")


def test_step_not_there_is_none(monkeypatch):
    sources(monkeypatch, {"portal": 404, "mirror": 404})
    assert ecmwf.step_fields(RUN, 3, BASES) is None


def test_gone_from_the_portal_while_the_mirror_fails_is_none(monkeypatch):
    # The portal dropped an old run and the mirror is throttling: the hours stay empty, the job goes on.
    sources(monkeypatch, {"portal": 404, "mirror": 503})
    assert ecmwf.step_fields(RUN, 3, BASES) is None


def test_every_source_failing_is_not_taken_for_not_there(monkeypatch):
    sources(monkeypatch, {"portal": 503, "mirror": OSError("no route")})
    with pytest.raises(ecmwf.SourceTrouble):
        ecmwf.step_fields(RUN, 3, BASES)


def test_forbidden_is_trouble_not_absence(monkeypatch):
    # A portal that refuses us (403) must not hide a run the mirror has.
    sources(monkeypatch, {"portal": 403, "mirror": INDEX})
    assert ecmwf.step_fields(RUN, 72, BASES, ask_all=False) is not None
    sources(monkeypatch, {"portal": 403, "mirror": 403})
    with pytest.raises(ecmwf.SourceTrouble):
        ecmwf.step_fields(RUN, 72, BASES, ask_all=False)


def test_latest_run_asks_the_portal_only_when_it_answers(monkeypatch):
    asked = sources(monkeypatch, {"portal": 404, "mirror": INDEX})
    assert ecmwf.latest_run(RUN + 3600, BASES) is None  # the portal says no run of the last 48 h is out
    assert all("//portal/" in url for url in asked)


def test_latest_run_falls_back_to_the_mirror(monkeypatch):
    sources(monkeypatch, {"portal": OSError("down"), "mirror": INDEX})
    assert ecmwf.latest_run(RUN + 3600, BASES) == RUN


def test_latest_run_with_nobody_answering_raises(monkeypatch):
    asked = sources(monkeypatch, {"portal": 500, "mirror": 503})
    with pytest.raises(ecmwf.SourceTrouble):
        ecmwf.latest_run(RUN + 3600, BASES)
    assert len(asked) == 2  # gives up at the first run instead of walking back 48 hours


def test_default_sources_are_the_portal_and_two_mirrors(monkeypatch):
    monkeypatch.delenv("ECMWF_SOURCES", raising=False)
    assert ecmwf.source_order() == [ecmwf.SOURCES["ecmwf"], ecmwf.SOURCES["aws"], ecmwf.SOURCES["gcs"]]
    monkeypatch.setenv("ECMWF_SOURCES", "gcs, https://mirror.example/data/")
    assert ecmwf.source_order() == [ecmwf.SOURCES["gcs"], "https://mirror.example/data"]


def test_download_moves_on_to_the_next_source(monkeypatch):
    message = b"GRIB" + b"x" * 12 + b"7777"
    asked = []

    def request(url, **kwargs):
        asked.append(url.split("/")[2])
        if "throttled" in url:
            raise HttpError(503, url, "SlowDown")
        if "gone" in url:
            raise HttpError(404, url)
        return 206, message

    monkeypatch.setattr(ecmwf, "request", request)
    field = ecmwf.Field("tp", "a/b.grib2", ("https://throttled", "https://gone", "https://fine"), 0, len(message))
    assert ecmwf.download(field) == message
    assert asked == ["throttled", "gone", "fine"]
    with pytest.raises(HttpError):
        ecmwf.download(ecmwf.Field("tp", "a/b.grib2", ("https://throttled", "https://gone"), 0, len(message)))
