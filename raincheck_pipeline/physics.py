"""Pure weather maths: no I/O, no third-party imports.

Everything here follows FAO Irrigation and Drainage Paper 56 (Allen et al., 1998), "FAO-56", with the
simplifications listed in the README.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

GSC = 0.0820  # solar constant, MJ m-2 min-1
SIGMA_HOUR = 2.043e-10  # Stefan-Boltzmann, MJ K-4 m-2 hour-1
ALBEDO = 0.23  # grass reference crop
WIND_10M_TO_2M = 4.87 / math.log(67.8 * 10.0 - 5.42)  # FAO-56 eq. 47, = 0.748
# Below this clear-sky radiation (MJ m-2 hour-1, about 80 W m-2) the measured Rs/Rso ratio is noise:
# the cloud cover gives the ratio instead (night and very low sun).
LOW_SUN_RSO = 0.3
# Rs/Rso when it is dark and there is no cloud cover either (the value FAO-56 example 19 assumes).
DARK_RATIO = 0.8


def per_step(acc: list[float | None], starts_at_zero: bool = True) -> list[float | None]:
    """Accumulated-since-start values at consecutive steps -> the amount inside each step.

    `acc[i]` is the accumulation at the end of step i. With `starts_at_zero` the accumulation before the
    first step is 0 (the forecast start), so the first amount is `acc[0]`; without it the first amount is
    unknown (None). A missing neighbour gives None. Packing noise can make a difference slightly
    negative: clipped to 0.
    """
    out: list[float | None] = []
    prev: float | None = 0.0 if starts_at_zero else None
    for value in acc:
        out.append(None if value is None or prev is None else max(0.0, value - prev))
        prev = value
    return out


def saturation_vapour_pressure(t_c: float) -> float:
    """e°(T) in kPa (FAO-56 eq. 11)."""
    return 0.6108 * math.exp(17.27 * t_c / (t_c + 237.3))


def relative_humidity(t_c: float | None, dew_c: float | None) -> float | None:
    """Relative humidity in % from air temperature and dew point (both °C)."""
    if t_c is None or dew_c is None:
        return None
    rh = 100.0 * saturation_vapour_pressure(dew_c) / saturation_vapour_pressure(t_c)
    return min(100.0, max(0.0, rh))


def wind_speed_kmh(u_ms: float | None, v_ms: float | None) -> float | None:
    """Wind speed in km/h from the u and v components in m/s."""
    if u_ms is None or v_ms is None:
        return None
    return math.hypot(u_ms, v_ms) * 3.6


def extraterrestrial_radiation(lat: float, lon: float, mid_utc: datetime, hours: float) -> float:
    """Ra over a period of `hours` centred on `mid_utc`, in MJ m-2 (whole period) (FAO-56 eq. 28).

    The period is cut at sunrise and sunset, so a step that straddles one of them only counts its
    daylight part. Solar time comes from UTC and the longitude (no time zones involved).
    """
    solar = mid_utc.astimezone(timezone.utc) + timedelta(hours=lon / 15.0)
    doy = solar.timetuple().tm_yday
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * doy / 365.0)  # eq. 23
    decl = 0.409 * math.sin(2.0 * math.pi * doy / 365.0 - 1.39)  # eq. 24
    b = 2.0 * math.pi * (doy - 81) / 364.0  # eq. 33
    sc = 0.1645 * math.sin(2.0 * b) - 0.1255 * math.cos(b) - 0.025 * math.sin(b)  # eq. 32, hours
    phi = math.radians(max(-89.99, min(89.99, lat)))
    # Sunset hour angle (eq. 25); the clamp covers polar day (pi) and polar night (0).
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(decl))))
    mid = mid_utc.astimezone(timezone.utc)
    solar_hours = mid.hour + mid.minute / 60.0 + mid.second / 3600.0 + lon / 15.0 + sc
    w = math.pi / 12.0 * ((solar_hours - 12.0) % 24.0)  # 0..2pi from solar noon
    if w > math.pi:
        w -= 2.0 * math.pi
    half = math.pi * hours / 24.0
    spans = [(w - half, w + half)]
    # A period across solar midnight wraps round: split it so each part lies inside [-pi, pi].
    if spans[0][0] < -math.pi:
        spans = [(-math.pi, w + half), (w - half + 2.0 * math.pi, math.pi)]
    elif spans[0][1] > math.pi:
        spans = [(w - half, math.pi), (-math.pi, w + half - 2.0 * math.pi)]
    total = 0.0
    for a, z in spans:
        w1, w2 = max(a, -ws), min(z, ws)
        if w2 <= w1:
            continue
        total += (12.0 * 60.0 / math.pi) * GSC * dr * (
            (w2 - w1) * math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * (math.sin(w2) - math.sin(w1))
        )
    return max(0.0, total)


def et0_fao56(
    *,
    t_c: float,
    ea_kpa: float,
    u2_ms: float,
    rs_mj: float,
    lat: float,
    lon: float,
    mid_utc: datetime,
    hours: float,
    cloud: float | None = None,
    elevation_m: float = 0.0,
) -> float:
    """Reference evapotranspiration over a period, in mm (FAO-56 Penman-Monteith, eq. 53 scaled to the period).

    t_c: mean air temperature of the period (°C); ea_kpa: actual vapour pressure (kPa); u2_ms: wind at
    2 m (m/s); rs_mj: solar radiation reaching the ground during the period (MJ m-2, whole period);
    cloud: total cloud cover 0..1 (only used when the sun is too low for Rs/Rso to mean anything).
    """
    rs = max(0.0, rs_mj) / hours  # MJ m-2 hour-1
    ra = extraterrestrial_radiation(lat, lon, mid_utc, hours) / hours
    rso = (0.75 + 2e-5 * elevation_m) * ra  # eq. 37
    if rso > LOW_SUN_RSO:
        ratio = min(1.0, max(0.3, rs / rso))
    elif cloud is not None:
        ratio = 1.0 - 0.75 * min(1.0, max(0.0, cloud)) ** 3.4  # Kasten & Czeplak (1980)
    else:
        ratio = DARK_RATIO
    es = saturation_vapour_pressure(t_c)
    ea = min(ea_kpa, es)
    rns = (1.0 - ALBEDO) * rs  # eq. 38
    rnl = SIGMA_HOUR * (t_c + 273.16) ** 4 * (0.34 - 0.14 * math.sqrt(ea)) * (1.35 * ratio - 0.35)  # eq. 39
    rn = rns - rnl
    g = (0.1 if rn > 0 else 0.5) * rn  # eq. 45 / 46: soil heat flux by day / by night
    delta = 4098.0 * es / (t_c + 237.3) ** 2  # eq. 13
    pressure = 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26  # eq. 7
    gamma = 0.000665 * pressure  # eq. 8
    et0_hour = (0.408 * delta * (rn - g) + gamma * 37.0 / (t_c + 273.0) * u2_ms * (es - ea)) / (
        delta + gamma * (1.0 + 0.34 * u2_ms)
    )
    return max(0.0, et0_hour) * hours
