"""The rain models' arithmetic: the common timeline, accumulations to 3-hour amounts, the GEFS chance.

No I/O. The timeline and the step lists are plain Python; the amounts work on numpy arrays (one value
per place), imported where they are made, so the check path never needs numpy.

Every model hands its rain over as accumulations, but each in its own way:
- GFS and ICON-EU: since the run started ("0-9 hour acc"): the amount in (L-3, L] is total(L) - total(L-3).
- GEFS: buckets that start again every 6 hours: at 3, 9, 15... h the bucket is the last 3 hours already;
  at 6, 12, 18... h it holds 6 hours, so the first 3 (the bucket of the step before) come off.
- AIFS: since the run started, but only every 6 hours: a 6-hour amount is split evenly over its two 3-hour
  steps (AIFS does not say how the rain fell inside those 6 hours, and halving keeps the total right).
The packing of the GRIB files is not exact (GEFS keeps 0.01 to 0.1 mm), so a difference can come out a
little below zero (down to -0.09 mm seen in GEFS): that is dry, and clipped at 0.
"""

from __future__ import annotations

import math
from typing import Callable

HOUR = 3600
STEP_H = 3
STEPS = 24  # 72 hours ahead, 3-hourly (docs/RAIN-TILES.md)

# A member "rains" in a step from 0.3 mm: the ECMWF ENS rule (ens.WET_MM), so the two ensembles pool.
WET_MM = 0.3
# GEFS has 31 members (control + 30). With fewer than this read in a step its chance and median are null.
MIN_MEMBERS = 25

# get(step) -> (hour the accumulation starts, values in mm at every place) or None when it was not read.
Getter = Callable[[int], "tuple[int, object] | None"]


def timeline(made: int) -> list[int]:
    """The 24 step ends of a tile: the end of the 3-hour step `made` falls in, then every 3 hours."""
    first = made - made % (STEP_H * HOUR) + STEP_H * HOUR
    return [first + k * STEP_H * HOUR for k in range(STEPS)]


def leads(run: int, times: list[int]) -> list[int]:
    """Hours after `run` of each step end. Every model has its own run, so the same `t` is a different
    lead for each: GFS of 00 UTC and ICON-EU of 06 UTC fill the same step from their steps 15 and 9."""
    return [(t - run) // HOUR for t in times]


def six_hour_window(lead: int) -> int:
    """The end (hours after the run) of the 6-hour AIFS step a 3-hour step lies in: 3 -> 6, 6 -> 6, 9 -> 12."""
    return lead if lead % 6 == 0 else lead + STEP_H


def needed_steps(kind: str, lead_hours: list[int], max_step: int) -> list[int]:
    """The forecast steps (hours) to download so that every step of the timeline has its amount.

    `kind` is "totals" (GFS, ICON-EU), "buckets" (GEFS) or "six_hourly" (AIFS). Step 0 is never
    fetched: nothing has fallen at the run's start. Steps past `max_step` (the model's last) are left out:
    those steps of the timeline stay null.
    """
    if kind not in ("totals", "buckets", "six_hourly"):
        raise ValueError(f"unknown kind {kind}")
    want: set[int] = set()
    for lead in lead_hours:
        if lead < STEP_H:
            continue
        if kind == "six_hourly":
            end = six_hour_window(lead)
            if end <= max_step:
                want.update({end, end - 6})
        elif lead <= max_step:
            want.add(lead)
            # GFS/ICON totals always need the step before; a GEFS bucket only at 6, 12, 18... h.
            if kind == "totals" or (kind == "buckets" and lead % 6 == 0):
                want.add(lead - STEP_H)
    return sorted(s for s in want if s > 0)


def _clip(np, values):
    """Negative packing noise to 0 (NaN stays NaN: not known)."""
    with np.errstate(invalid="ignore"):
        return np.where(values < 0, 0.0, values)


def three_hourly(lead_hours: list[int], get: Getter, max_step: int) -> list:
    """The amount (mm) in each 3-hour step from accumulations that end every 3 hours (GFS, GEFS, ICON-EU).

    The accumulation ending at the step says where it starts: right at the step's start (GEFS at 3, 9,
    15... h, any model's step 3) and it is the amount; earlier, and the accumulation of the step before
    (which must start at the same hour) comes off. Returns one array per step, or None where the run
    doesn't cover it or a field is missing.
    """
    import numpy as np

    out: list = []
    for lead in lead_hours:
        if lead < STEP_H or lead > max_step:
            out.append(None)
            continue
        cur = get(lead)
        if cur is None:
            out.append(None)
            continue
        start, values = cur
        if start == lead - STEP_H:
            out.append(_clip(np, values))
            continue
        prev = get(lead - STEP_H)
        if prev is None or prev[0] != start:
            out.append(None)  # not the same accumulation: no difference to take
            continue
        out.append(_clip(np, values - prev[1]))
    return out


def six_hourly_halves(lead_hours: list[int], get: Getter, max_step: int) -> list:
    """AIFS: totals since the run's start every 6 hours; each 3-hour step gets half of its 6-hour amount."""
    import numpy as np

    out: list = []
    for lead in lead_hours:
        end = six_hour_window(lead)
        if lead < STEP_H or end > max_step:
            out.append(None)
            continue
        cur = get(end)
        prev = (0, 0.0) if end == 6 else get(end - 6)
        if cur is None or prev is None:
            out.append(None)
            continue
        out.append(_clip(np, cur[1] - prev[1]) / 2.0)
    return out


def ensemble(per_member: list[list], size: int, wet_mm: float = WET_MM, min_members: int = MIN_MEMBERS):
    """GEFS: (chance %, median mm) per step and place from every member's 3-hour amounts.

    `per_member[m][k]` is member m's amount array at step k (None: not read). Returns two float arrays
    [steps, places]: the share of the members read with at least `wet_mm` in the step, and their median
    amount; both NaN where fewer than `min_members` members are known at that place and step.
    """
    import numpy as np

    steps = len(per_member[0]) if per_member else 0
    chance = np.full((steps, size), np.nan, dtype=np.float64)
    median = np.full((steps, size), np.nan, dtype=np.float64)
    for k in range(steps):
        rows = [m[k] for m in per_member if m[k] is not None]
        if len(rows) < min_members:
            continue
        stack = np.vstack(rows).astype(np.float64)  # [members, places]
        known = ~np.isnan(stack)
        n = known.sum(axis=0)
        wet = ((stack >= wet_mm) & known).sum(axis=0)
        enough = n >= min_members
        with np.errstate(invalid="ignore", divide="ignore"):
            chance[k] = np.where(enough, 100.0 * wet / np.maximum(n, 1), np.nan)
        if enough.any():
            med = np.full(size, np.nan)
            med[enough] = np.nanmedian(stack[:, enough], axis=0)
            median[k] = med
    return chance, median


def mm(value) -> float | int | None:
    """An amount as the tile writes it: mm rounded to 0.1, never negative, null when not known."""
    if value is None:
        return None
    v = float(value)
    if not math.isfinite(v):
        return None
    r = round(max(0.0, v), 1)
    return int(r) if r == int(r) else r


def percent(value) -> int | None:
    """A chance as the tile writes it: whole %, 0 to 100, null when not known."""
    if value is None:
        return None
    v = float(value)
    if not math.isfinite(v):
        return None
    return int(round(min(100.0, max(0.0, v))))
