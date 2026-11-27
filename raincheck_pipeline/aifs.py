"""ECMWF AIFS single (the machine-learned model, 0.25°): its rain, 6-hourly, from ECMWF open data.

Layout: the IFS one (ecmwf.py) with `aifs-single` in place of `ifs`, only in `oper`, on the same portal
and mirrors (AWS, Google Cloud):
  <base>/<YYYYMMDD>/<HH>z/aifs-single/0p25/oper/<YYYYMMDDHH>0000-<step>h-oper-fc.grib2 (+ .index)
Only `tp` is fetched by range request (about 0.3 MB a step). The steps are 6-hourly: 12 or 13 of them,
about 4 MB and 26 requests a run.

`tp` is accumulated since the run started, in kg m-2 (mm; IFS writes metres, rain.py reads the units);
a 6-hour amount is split evenly over its two 3-hour steps (rainmath.six_hourly_halves). Same grid as IFS
(longitudes from 180 W); the land-sea mask `lsm` is in step 0. Runs at 00, 06, 12, 18 UTC; the 72 h step
is on the portal about 6.1 hours after the run (27/11/2026, 00 UTC). Licence CC BY 4.0 (ECMWF).
Standard library only.
"""

from __future__ import annotations

from . import ecmwf

NAME = "aifs"
KIND = "six_hourly"
FOLDER = "aifs-single"
STREAMS = ("oper",)
EVERY_H = 6
MAX_STEP_H = 360
LOOK_BACK_H = 18
MEMBERS = None


def runs(now: int) -> list[int]:
    newest = now - now % (EVERY_H * 3600)
    return [newest - h * 3600 for h in range(0, LOOK_BACK_H + 1, EVERY_H)]


def is_out(run: int, steps: list[int]) -> bool:
    """Whether the run's last needed step has its `tp` (raises ecmwf.SourceTrouble when nobody answers).

    ECMWF publishes a run's steps in order (the base layer relies on it too), so the last one is enough.
    The first source that says "not there" settles it, as for the base layer's newest run: the portal
    gets the files first.
    """
    if not steps:
        return False
    fields = ecmwf.step_fields(run, steps[-1], ask_all=False, params=("tp",), model=FOLDER, streams=STREAMS)
    return fields is not None and "tp" in fields


def fetch(run: int, step: int, member: int | None = None) -> tuple[int, bytes] | None:
    """(0: accumulated since the run's start, the `tp` message) of one step; None when it is not there."""
    fields = ecmwf.step_fields(run, step, params=("tp",), model=FOLDER, streams=STREAMS)
    if not fields or "tp" not in fields:
        return None
    return 0, ecmwf.download(fields["tp"])


def land(run: int) -> bytes | None:
    """The land-sea mask (`lsm`) of step 0; None when the run has none."""
    fields = ecmwf.step_fields(run, 0, params=("lsm",), model=FOLDER, streams=STREAMS)
    return ecmwf.download(fields["lsm"]) if fields and "lsm" in fields else None
