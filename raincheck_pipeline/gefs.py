"""NOAA GEFS (0.25°, 31 members): every member's rain per step, for the rain chance and the median.

Layout (NOAA Big Data Program on AWS, same `.idx` inventories as GFS: noaa.py):
  https://noaa-gefs-pds.s3.amazonaws.com/gefs.<YYYYMMDD>/<HH>/atmos/pgrb2sp25/<member>.t<HH>z.pgrb2s.0p25.f<FFF>
  members gec00 (control) and gep01 to gep30. One small file per member and step (about 10 MB); only its
  APCP record is fetched (about 0.3 MB). 31 members x 24 or 25 steps: about 1,500 requests (inventory +
  record) and 215 MB a run (measured 27/11/2026: 214 MB), whatever the number of places.

APCP here is only the bucket that starts again every 6 hours (no run total as in GFS): at 3, 9, 15... h
it is the last 3 hours; at 6, 12, 18... h six, from which the bucket of the step before comes off
(rainmath.three_hourly). The packing keeps 0.01 to 0.1 mm, so a difference can be a little negative
(down to -0.09 mm seen): clipped at 0. Same grid as GFS (longitudes 0 to 359.75 E), so the GFS land-sea
mask of the same run is used. 3-hourly to 240 h; every member's 72 h step is out about 4.4 hours after
the run (27/11/2026, 00 UTC). NOAA data is in the public domain. Standard library only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import gfs, noaa
from .ecmwf import SourceTrouble
from .http import HttpError

NAME = "gefs"
KIND = "buckets"
BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
EVERY_H = 6
MAX_STEP_H = 240  # 3-hourly to 240 h, 6-hourly after
LOOK_BACK_H = 18
MEMBERS = list(range(31))  # 0 = the control gec00, 1..30 = gep01..gep30


def member_name(member: int) -> str:
    return "gec00" if member == 0 else f"gep{member:02d}"


def file_url(run: int, step: int, member: int) -> str:
    d = datetime.fromtimestamp(run, tz=timezone.utc)
    return f"{BASE}/gefs.{d:%Y%m%d}/{d:%H}/atmos/pgrb2sp25/{member_name(member)}.t{d:%H}z.pgrb2s.0p25.f{step:03d}"


def runs(now: int) -> list[int]:
    newest = now - now % (EVERY_H * 3600)
    return [newest - h * 3600 for h in range(0, LOOK_BACK_H + 1, EVERY_H)]


def is_out(run: int, steps: list[int]) -> bool:
    """Whether every needed step of the control and of the last member is out (HEAD requests, the last
    step first: noaa.all_exist).

    The members are computed side by side and land within minutes of each other; the job counts what
    it reads anyway (a member with a step missing is left out; with fewer than 25 left, GEFS is).
    """
    try:
        return all(noaa.all_exist([file_url(run, s, m) + ".idx" for s in steps]) for m in (MEMBERS[0], MEMBERS[-1]))
    except (HttpError, OSError) as err:
        raise SourceTrouble(f"GEFS could not be asked: {err}") from None


def fetch(run: int, step: int, member: int | None = 0) -> tuple[int, bytes] | None:
    """(hour the bucket starts, the APCP record) of one member and step; None when it is not there."""
    url = file_url(run, step, member or 0)
    records = noaa.read_idx(url + ".idx")
    if records is None:
        return None
    found = noaa.rain_record(records, step, run_total=False)
    if found is None:
        return None
    record, start = found
    return start, noaa.download(url, record)


def land(run: int) -> bytes | None:
    """GFS's land-sea mask of the same run (GEFS's own files have none; the grid is the same)."""
    return gfs.land(run)
