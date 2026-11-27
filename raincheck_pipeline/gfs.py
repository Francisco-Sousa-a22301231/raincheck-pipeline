"""NOAA GFS (0.25°, deterministic): its rain, step by step, from the AWS open-data bucket.

Layout (NOAA Big Data Program, no key, no mirror needed: the bucket is NOAA's own distribution):
  https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.<YYYYMMDD>/<HH>/atmos/gfs.t<HH>z.pgrb2.0p25.f<FFF>
  (about 500 MB a step) and the same name ending in .idx (noaa.py). Only the precipitation record is
  fetched, about 0.3 MB a step: 24 or 25 steps, about 14 MB and 50 requests a run.

Every step has two APCP records: the bucket that starts again every 6 hours and the total since the run
started ("0-9 hour acc", at whole days "0-1 day acc", "0-3 day acc"); the run total is taken, so a step's
amount is total(L) - total(L-3) (noaa.rain_record, rainmath.three_hourly). Values are kg m-2 = mm.
The grid is 1440 x 721, 90 N to 90 S, longitudes 0 to 359.75 E (ECMWF's start at 180 W: grib.py works
either way). Runs at 00, 06, 12, 18 UTC, hourly steps to 120 h; the 72 h step is out about 4.1 hours
after the run (measured 27/11/2026, 00 UTC run). The land-sea mask is the `LAND` record of step 0.
NOAA data is in the public domain. Standard library only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import noaa
from .ecmwf import SourceTrouble
from .http import HttpError

NAME = "gfs"
KIND = "totals"
BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
EVERY_H = 6
MAX_STEP_H = 384  # every 3 h is there to 384 h (hourly to 120 h)
LOOK_BACK_H = 18  # how far back a complete run is looked for
MEMBERS = None  # deterministic: one "member"


def file_url(run: int, step: int) -> str:
    d = datetime.fromtimestamp(run, tz=timezone.utc)
    return f"{BASE}/gfs.{d:%Y%m%d}/{d:%H}/atmos/gfs.t{d:%H}z.pgrb2.0p25.f{step:03d}"


def runs(now: int) -> list[int]:
    """The runs to try, newest first."""
    newest = now - now % (EVERY_H * 3600)
    return [newest - h * 3600 for h in range(0, LOOK_BACK_H + 1, EVERY_H)]


def is_out(run: int, steps: list[int]) -> bool:
    """Whether every needed step of the run is published (its inventory, written after the file).

    The last step first, then the others (noaa.all_exist: GFS steps do not come out in order). Raises
    SourceTrouble when the bucket cannot be asked (an answer that is neither yes nor no).
    """
    try:
        return noaa.all_exist([file_url(run, s) + ".idx" for s in steps])
    except (HttpError, OSError) as err:
        raise SourceTrouble(f"GFS could not be asked: {err}") from None


def fetch(run: int, step: int, member: int | None = None) -> tuple[int, bytes] | None:
    """(hour its accumulation starts, the APCP run-total record) of one step; None when it is not there."""
    url = file_url(run, step)
    records = noaa.read_idx(url + ".idx")
    if records is None:
        return None
    found = noaa.rain_record(records, step, run_total=True)
    if found is None:
        return None
    record, start = found
    return start, noaa.download(url, record)


def land(run: int) -> bytes | None:
    """The land-sea mask (`LAND`, 0 or 1) from step 0 of the run; None when the run has none."""
    url = file_url(run, 0)
    records = noaa.read_idx(url + ".idx") or []
    for r in records:
        if r.var == "LAND" and r.level == "surface":
            return noaa.download(url, r)
    return None
