"""ECMWF open data (IFS, 0.25 degrees): finding the newest run and downloading single fields.

Layout (the same on the ECMWF portal and on its mirrors at AWS and Google Cloud):
  <base>/<YYYYMMDD>/<HH>z/ifs/0p25/<stream>/<YYYYMMDDHH>0000-<step>h-<stream>-fc.grib2  (about 100 MB)
  and the same name ending in .index: one JSON line per field with its `_offset` and `_length`.
Only the fields we need are fetched, with HTTP range requests. Standard library only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .http import HttpError, request
from .tiles import HOUR, PARAMS, RUN_EVERY_H, SPAN_H

# The portal only keeps the last four dates: the oldest run an 18 UTC run needs (72 h back, read after
# midnight) is already gone from it and always comes from a mirror. The AWS bucket often answers
# 503 "SlowDown", hence a second mirror.
SOURCES = {
    "ecmwf": "https://data.ecmwf.int/forecasts",
    "aws": "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "gcs": "https://storage.googleapis.com/ecmwf-open-data",
}
DEFAULT_SOURCES = "ecmwf,aws,gcs"
# All four daily runs are in `oper` today; the 06 and 18 UTC runs used to be in `scda`.
STREAMS = ("oper", "scda")
# How far back to look for a complete run before giving up (a run is out 7 to 9 hours after its time).
LOOK_BACK_H = 48


def source_order() -> list[str]:
    """Base URLs to try, in order (`ECMWF_SOURCES`, default "ecmwf,aws,gcs": names from SOURCES or URLs)."""
    names = [s.strip() for s in (os.environ.get("ECMWF_SOURCES") or DEFAULT_SOURCES).split(",") if s.strip()]
    return [SOURCES.get(n, n).rstrip("/") for n in names]


def file_path(run: int, step: int, stream: str, ext: str, model: str = "ifs") -> str:
    """`model` is the folder of the forecast system: "ifs" (the base layer) or "aifs-single" (rain models)."""
    d = datetime.fromtimestamp(run, tz=timezone.utc)
    return f"{d:%Y%m%d}/{d:%H}z/{model}/0p25/{stream}/{d:%Y%m%d%H}0000-{step}h-{stream}-fc.{ext}"


@dataclass(frozen=True)
class Field:
    param: str
    path: str  # of the .grib2 file, under a source's base URL
    bases: tuple[str, ...]  # sources to ask, in order (the files are identical copies)
    offset: int
    length: int


def parse_index(text: str, path: str, bases: tuple[str, ...], params: tuple[str, ...] = PARAMS) -> dict[str, Field]:
    """The surface fields we need out of an .index file: `{ param: Field }`."""
    out: dict[str, Field] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("levtype") == "sfc" and row.get("type") == "fc" and row.get("param") in params:
            out[row["param"]] = Field(row["param"], path, bases, int(row["_offset"]), int(row["_length"]))
    return out


class SourceTrouble(OSError):
    """No source could say whether a step exists (errors or no connection everywhere)."""


def step_fields(
    run: int, step: int, sources: list[str] | None = None, ask_all: bool = True, params: tuple[str, ...] = PARAMS,
    model: str = "ifs", streams: tuple[str, ...] = STREAMS,
) -> dict[str, Field] | None:
    """Where the needed fields of one step are, from the first source that has the step.

    None when a source answered "not there" (404) and no other has it: not published yet, or already
    gone. A source that fails is reported and the next one is asked; when every source failed nobody
    knows whether the step exists, and that raises SourceTrouble instead of passing for "not there"
    (a step taken for gone is uploaded as empty hours). With `ask_all` off, the first source that
    answers "not there" settles it (right for a run that may not be out yet; wrong for old runs, which
    the origin drops first). `model` and `streams` pick another forecast system with the same layout
    (AIFS: "aifs-single", only in `oper`).
    """
    bases = sources or source_order()
    said_no = False
    for base in bases:
        missing = 0
        for stream in streams:
            url = f"{base}/{file_path(run, step, stream, 'index', model)}"
            try:
                _, body = request(url)
            except HttpError as err:
                if err.status == 404:  # both the portal and the mirror answer 404 for a file they do not have
                    missing += 1
                    continue
                print(f"  warning: {base} answered HTTP {err.status} for step {step} of run {run_name(run)}", flush=True)
                break  # this source is in trouble: ask the next one
            except OSError as err:
                print(f"  warning: {base} could not be reached ({err})", flush=True)
                break
            order = (base, *[b for b in bases if b != base])
            return parse_index(body.decode("utf-8"), file_path(run, step, stream, "grib2", model), order, params)
        if missing == len(streams):
            said_no = True
            if not ask_all:
                return None
    if not said_no:
        raise SourceTrouble(f"no source could be asked for step {step} of run {run_name(run)}")
    return None


def download(field: Field) -> bytes:
    """One GRIB message, by range request (from the next source when one fails)."""
    last = field.offset + field.length - 1
    failure: Exception | None = None
    for base in field.bases:
        url = f"{base}/{field.path}"
        try:
            status, body = request(url, headers={"Range": f"bytes={field.offset}-{last}"})
        except (HttpError, OSError) as err:
            failure = err
            continue
        if status == 206 and len(body) == field.length and body[:4] == b"GRIB" and body[-4:] == b"7777":
            return body
        failure = ValueError(f"bad range answer for {field.param} in {url} (status {status}, {len(body)} bytes)")
    assert failure is not None
    raise failure


def latest_run(now: int, sources: list[str] | None = None) -> int | None:
    """The newest run (unix s) whose last needed step (72 h) is published with all seven fields."""
    newest = now - now % (RUN_EVERY_H * HOUR)
    for back in range(0, LOOK_BACK_H + 1, RUN_EVERY_H):
        run = newest - back * HOUR
        fields = step_fields(run, SPAN_H, sources, ask_all=False)
        if fields is not None and all(p in fields for p in PARAMS):
            return run
    return None


def run_name(run: int) -> str:
    return datetime.fromtimestamp(run, tz=timezone.utc).strftime("%Y-%m-%d %H UTC")
