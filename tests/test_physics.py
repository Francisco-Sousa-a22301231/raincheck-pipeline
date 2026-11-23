"""Checks of the weather maths against FAO-56 (Allen et al., 1998) worked examples and tables."""

from datetime import datetime, timezone

import pytest

from raincheck_pipeline import physics

# FAO-56 example 19 (chapter 4, "Determination of ETo for hourly time step"): N'Diaye, Senegal,
# 16°13'N 16°15'W, 8 m above sea level, 1 October. The local clock is the 15°W time zone, i.e. UTC-1.
LAT, LON, Z = 16 + 13 / 60, -(16 + 15 / 60), 8.0


def test_per_step_from_accumulated():
    assert physics.per_step([0.001, 0.003, 0.003, 0.0045]) == pytest.approx([0.001, 0.002, 0.0, 0.0015])


def test_per_step_missing_and_negative():
    # A missing value hides its own step and the next one; packing noise never gives negative rain.
    assert physics.per_step([1.0, None, 3.0, 2.9]) == [1.0, None, None, 0.0]
    assert physics.per_step([1.0, 4.0], starts_at_zero=False) == [None, 3.0]


def test_relative_humidity_from_dew_point():
    # FAO-56 annex 2, table 2.3: e°(25 °C) = 3.168 kPa, e°(14 °C) = 1.599 kPa -> 50.5 %.
    assert physics.saturation_vapour_pressure(25.0) == pytest.approx(3.168, abs=0.001)
    assert physics.saturation_vapour_pressure(14.0) == pytest.approx(1.599, abs=0.001)
    assert physics.relative_humidity(25.0, 14.0) == pytest.approx(50.5, abs=0.1)
    assert physics.relative_humidity(18.0, 18.0) == pytest.approx(100.0)
    assert physics.relative_humidity(10.0, 12.0) == 100.0  # dew point above temperature: capped
    assert physics.relative_humidity(None, 12.0) is None


def test_wind_speed():
    assert physics.wind_speed_kmh(3.0, -4.0) == pytest.approx(18.0)
    assert physics.wind_speed_kmh(None, 1.0) is None
    assert physics.WIND_10M_TO_2M == pytest.approx(0.748, abs=0.001)


def test_extraterrestrial_radiation_example_19():
    # 14:00-15:00 local = 15:00-16:00 UTC. The book gives Ra = 3.543 MJ m-2 hour-1.
    mid = datetime(2021, 10, 1, 15, 30, tzinfo=timezone.utc)
    assert physics.extraterrestrial_radiation(LAT, LON, mid, 1.0) == pytest.approx(3.543, abs=0.02)


def test_et0_example_19_afternoon():
    # T 38 °C, RH 52 %, u2 3.3 m/s, Rs 2.450 MJ m-2 hour-1 -> ETo = 0.63 mm/hour.
    ea = 0.52 * physics.saturation_vapour_pressure(38.0)
    et0 = physics.et0_fao56(
        t_c=38.0, ea_kpa=ea, u2_ms=3.3, rs_mj=2.450, lat=LAT, lon=LON,
        mid_utc=datetime(2021, 10, 1, 15, 30, tzinfo=timezone.utc), hours=1.0, elevation_m=Z,
    )
    assert et0 == pytest.approx(0.63, abs=0.01)


def test_et0_example_19_night():
    # 02:00-03:00 local: T 28 °C, RH 90 %, u2 1.9 m/s, Rs 0, Rs/Rso taken as 0.8 -> ETo = 0.00 mm/hour.
    ea = 0.90 * physics.saturation_vapour_pressure(28.0)
    et0 = physics.et0_fao56(
        t_c=28.0, ea_kpa=ea, u2_ms=1.9, rs_mj=0.0, lat=LAT, lon=LON,
        mid_utc=datetime(2021, 10, 1, 3, 30, tzinfo=timezone.utc), hours=1.0, elevation_m=Z,
    )
    assert round(et0, 2) == 0.00
    assert et0 >= 0.0


def test_three_hour_period_is_the_sum_of_its_hours():
    hours = [datetime(2021, 10, 1, h, 30, tzinfo=timezone.utc) for h in (15, 16, 17)]
    by_hour = sum(physics.extraterrestrial_radiation(LAT, LON, m, 1.0) for m in hours)
    whole = physics.extraterrestrial_radiation(LAT, LON, datetime(2021, 10, 1, 16, 30, tzinfo=timezone.utc), 3.0)
    assert whole == pytest.approx(by_hour, rel=1e-6)


def test_extraterrestrial_radiation_day_and_night():
    night = datetime(2021, 10, 1, 1, 0, tzinfo=timezone.utc)
    assert physics.extraterrestrial_radiation(LAT, LON, night, 3.0) == 0.0
    # A step across sunset only counts its daylight part.
    sunset = datetime(2021, 10, 1, 19, 0, tzinfo=timezone.utc)
    part = physics.extraterrestrial_radiation(LAT, LON, sunset, 3.0)
    assert 0.0 < part < physics.extraterrestrial_radiation(LAT, LON, datetime(2021, 10, 1, 16, 0, tzinfo=timezone.utc), 3.0)


def test_extraterrestrial_radiation_polar():
    # Midnight sun (period across solar midnight) and polar night.
    assert physics.extraterrestrial_radiation(80.0, 0.0, datetime(2021, 6, 21, 0, 0, tzinfo=timezone.utc), 3.0) > 0.5
    assert physics.extraterrestrial_radiation(80.0, 0.0, datetime(2021, 12, 21, 12, 0, tzinfo=timezone.utc), 3.0) == 0.0
    # A whole day at the equator in mid-March is about 37.9 MJ m-2 (FAO-56 annex 2, table 2.6).
    day = sum(physics.extraterrestrial_radiation(0.0, 0.0, datetime(2021, 3, 15, h, 30, tzinfo=timezone.utc), 3.0) for h in range(1, 24, 3))
    assert day == pytest.approx(37.9, abs=0.5)
