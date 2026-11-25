"""ECMWF ENS (the 50-member ensemble, 0.25 degrees): the rain chance of every 3-hour step ahead.

Layout (the same on the ECMWF portal and on its mirrors at AWS and Google Cloud):
  <base>/<YYYYMMDD>/<HH>z/ifs/0p25/enfo/<YYYYMMDDHH>0000-<step>h-enfo-ef.grib2  (about 4.5 GB per step)
  and the same name ending in .index (about 1 MB). Only the 50 perturbed members' total precipitation `tp`
  is fetched, by range request: about 0.45 MB each, 50 requests per step (the members are not stored next
  to each other), 24 steps (3 to 72 h): about 550 MB and 1,224 requests per run, whatever the number of
  places (measured 25/11/2026: 552 MB).

The ENS runs at the same times as IFS HRES, on the same 0.25° grid (so a place reads the same grid point
as for the base layer), 3-hourly to 144 h, and is out a few minutes after it (25/11/2026, 00 UTC run, 72 h
step: HRES 07:34, ENS 07:40 UTC on the portal; AWS a minute or two later, Google Cloud 14 to 60 minutes
later). Standard library only here; numpy is imported where the numbers are made.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .ecmwf import Field, SourceTrouble, run_name, source_order
from .http import HttpError, request
from .tiles import SPAN_H, STEP_H

# A member "rains" in a 3-hour step from 0.3 mm in the step: 0.1 mm an hour on average, Open-Meteo's own bar
# for precipitation_probability (members with more than 0.1 mm in the hour), so the chance reads like the
# one Rain Check's cards show for a zone with its own forecast. Measured 25/11/2026 against Open-Meteo's
# ecmwf_ifs025 chance at 12 wet Iberian places (00 UTC run, 72 h ahead): a bar of 0.1 mm/3h reads 12.9
# points too high, 0.2 +4.1, 0.3 -2.9 (mean gap 8.8 points), 0.5 -10.8.
WET_MM = 0.3
# Fewer members than this in a step and its chance is not given (null): ECMWF publishes 50.
MIN_MEMBERS = 40
# How long after the run's time the base layer waits for the ENS before it is built without it (hours).
# HRES and ENS come out minutes apart (about 7.5 h after the run), so this only matters when the ENS is
# late or missing: the run is then built with the rain chance left out (the server falls back to its own).
WAIT_H = 10


def ens_path(run: int, step: int, ext: str) -> str:
    d = datetime.fromtimestamp(run, tz=timezone.utc)
    return f"{d:%Y%m%d}/{d:%H}z/ifs/0p25/enfo/{d:%Y%m%d%H}0000-{step}h-enfo-ef.{ext}"


def steps() -> list[int]:
    """3, 6, ... 72 (at step 0 nothing has fallen yet)."""
    return list(range(STEP_H, SPAN_H + STEP_H, STEP_H))


def parse_members(text: str, path: str, bases: tuple[str, ...], param: str = "tp") -> list[Field]:
    """The `tp` field of every member (perturbed `pf`, and control `cf` if the file has one), by member number."""
    out: list[tuple[int, Field]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("levtype") == "sfc" and row.get("param") == param and row.get("type") in ("pf", "cf"):
            number = int(row.get("number") or 0)
            out.append((number, Field(f"{param}#{number}", path, bases, int(row["_offset"]), int(row["_length"]))))
    return [f for _, f in sorted(out, key=lambda x: x[0])]


def step_members(run: int, step: int, sources: list[str] | None = None) -> list[Field] | None:
    """Where every member's tp of one step is, from the first source that has the step.

    None when the sources answered "not there" (404); raises SourceTrouble when none could be asked.
    Every source is asked: the mirrors get the files later than the portal, and keep them longer.
    """
    bases = sources or source_order()
    said_no = False
    for base in bases:
        url = f"{base}/{ens_path(run, step, 'index')}"
        try:
            _, body = request(url)
        except HttpError as err:
            if err.status == 404:
                said_no = True
                continue
            print(f"  warning: {base} answered HTTP {err.status} for ENS step {step} of run {run_name(run)}", flush=True)
            continue
        except OSError as err:
            print(f"  warning: {base} could not be reached ({err})", flush=True)
            continue
        order = (base, *[b for b in bases if b != base])
        return parse_members(body.decode("utf-8"), ens_path(run, step, "grib2"), order)
    if not said_no:
        raise SourceTrouble(f"no source could be asked for ENS step {step} of run {run_name(run)}")
    return None


def is_out(run: int, sources: list[str] | None = None) -> bool:
    """Whether the run's last needed ENS step (72 h) is published: a HEAD request per source until one has it.

    A source in trouble counts as "not there": the base layer then waits for the ENS (WAIT_H), and is
    built without the rain chance after that, rather than not at all.
    """
    for base in sources or source_order():
        try:
            request(f"{base}/{ens_path(run, SPAN_H, 'index')}", method="HEAD")
            return True
        except HttpError as err:
            if err.status != 404:
                print(f"  warning: {base} answered HTTP {err.status} for the ENS of run {run_name(run)}", flush=True)
        except OSError as err:
            print(f"  warning: {base} could not be reached ({err})", flush=True)
    return False


def chance(acc, wet_mm: float = WET_MM):
    """Rain chance (%) per step and point from the members' accumulations.

    `acc[k]` is a float array [members, points] of tp (metres, accumulated since the run's start) at
    steps()[k], NaN where a member could not be read. The amount in a step is the difference of two
    accumulations (step 3 starts from zero); tiny negative steps from packing count as dry. Returns a
    float array [steps, points]: the share of the members that hold at least `wet_mm`, NaN where fewer
    than MIN_MEMBERS members are known.
    """
    import numpy as np

    out = []
    prev = None
    for cur in acc:
        mm = (cur if prev is None else cur - prev) * 1000.0
        prev = cur
        known = ~np.isnan(mm)
        n = known.sum(axis=0)
        wet = ((mm >= wet_mm) & known).sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            p = np.where(n >= MIN_MEMBERS, np.round(100.0 * wet / np.maximum(n, 1)), np.nan)
        out.append(p)
    return np.array(out)
