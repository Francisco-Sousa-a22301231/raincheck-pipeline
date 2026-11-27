"""DWD ICON-EU (0.0625°, about 7 km, Europe only): its rain, step by step, from DWD's open-data server.

Layout (no key, no mirror; each <HH>/ folder holds only the newest run of that hour, for about 24 h):
  https://opendata.dwd.de/weather/nwp/icon-eu/grib/<HH>/tot_prec/
      icon-eu_europe_regular-lat-lon_single-level_<YYYYMMDDHH>_<FFF>_TOT_PREC.grib2.bz2
  one whole file per step (bzip2, 0.5 to 0.8 MB): 24 or 25 requests, about 16 MB a run. The land
  fraction is in <HH>/fr_land/icon-eu_europe_regular-lat-lon_time-invariant_<YYYYMMDDHH>_FR_LAND.grib2.bz2.

TOT_PREC is kg m-2 (mm) since the run started, so a step's amount is total(L) - total(L-3). The grid
(read from the GRIB) is 1377 x 657, 29.5 to 70.5 N, 23.5 W to 62.5 E (written 336.5 E), south to north:
places outside it get no `icon_eu` at all. ICON-EU runs every 3 hours; the 00, 06, 12 and 18 UTC runs go
to 120 h (the others only to 30 h) and are the ones used. The 72 h step is out about 3.3 hours after the
run (27/11/2026, 00 UTC). Licence CC BY 4.0, "Source: Deutscher Wetterdienst". Standard library only.
"""

from __future__ import annotations

import bz2
from datetime import datetime, timezone

from .ecmwf import SourceTrouble
from .http import HttpError, request

NAME = "icon_eu"
KIND = "totals"
BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
EVERY_H = 6
MAX_STEP_H = 120
LOOK_BACK_H = 18
MEMBERS = None


def _stamp(run: int) -> tuple[str, str]:
    d = datetime.fromtimestamp(run, tz=timezone.utc)
    return f"{d:%H}", f"{d:%Y%m%d%H}"


def file_url(run: int, step: int) -> str:
    hh, stamp = _stamp(run)
    return f"{BASE}/{hh}/tot_prec/icon-eu_europe_regular-lat-lon_single-level_{stamp}_{step:03d}_TOT_PREC.grib2.bz2"


def land_url(run: int) -> str:
    hh, stamp = _stamp(run)
    return f"{BASE}/{hh}/fr_land/icon-eu_europe_regular-lat-lon_time-invariant_{stamp}_FR_LAND.grib2.bz2"


def runs(now: int) -> list[int]:
    newest = now - now % (EVERY_H * 3600)
    return [newest - h * 3600 for h in range(0, LOOK_BACK_H + 1, EVERY_H)]


def _there(url: str) -> bool:
    try:
        request(url, method="HEAD")
        return True
    except HttpError as err:
        if err.status == 404:
            return False
        raise SourceTrouble(f"DWD answered HTTP {err.status}") from None
    except OSError as err:
        raise SourceTrouble(f"DWD could not be reached: {err}") from None


def is_out(run: int, steps: list[int]) -> bool:
    """Whether every needed step of the run is on the server (HEAD requests, the last step first, then
    the others side by side). A run older than a day has been replaced by the next one of the same hour
    and answers 404, like one not out yet."""
    from concurrent.futures import ThreadPoolExecutor

    if not steps or not _there(file_url(run, steps[-1])):
        return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        return all(pool.map(_there, [file_url(run, s) for s in steps[:-1]]))


def _get(url: str) -> bytes | None:
    try:
        _, body = request(url)
    except HttpError as err:
        if err.status == 404:
            return None
        raise
    message = bz2.decompress(body)
    if message[:4] != b"GRIB" or message[-4:] != b"7777":
        raise ValueError(f"{url} is not one GRIB message")
    return message


def fetch(run: int, step: int, member: int | None = None) -> tuple[int, bytes] | None:
    """(0: accumulated since the run's start, the decompressed GRIB message) of one step; None: not there."""
    message = _get(file_url(run, step))
    return None if message is None else (0, message)


def land(run: int) -> bytes | None:
    """The land fraction (0 to 1) of the run's grid; None when DWD has none for it."""
    return _get(land_url(run))
