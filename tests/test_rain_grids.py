"""Reading a place on the rain models' grids: GFS's longitudes 0..360 against ECMWF's -180..180, ICON-EU's edge."""

import pytest

from raincheck_pipeline.grib import Grid, contains, nearest_index, point_of

GFS = Grid(ni=1440, nj=721, lat_first=90.0, lon_first=0.0, d_lat=0.25, d_lon=0.25)
IFS = Grid(ni=1440, nj=721, lat_first=90.0, lon_first=180.0, d_lat=0.25, d_lon=0.25)
# ICON-EU as its GRIB says: south to north, first longitude 336.5 (= 23.5 W).
ICON = Grid(ni=1377, nj=657, lat_first=29.5, lon_first=336.5, d_lat=0.0625, d_lon=0.0625, north_to_south=False)

PLACES = [(38.70, -9.38), (38.976, -9.418), (33.985, -118.4695), (-33.8915, 151.2767), (51.5, -0.1), (51.5, 0.1)]


def test_gfs_and_ifs_grids_give_the_same_point():
    for lat, lon in PLACES:
        assert point_of(GFS, nearest_index(GFS, lat, lon)) == point_of(IFS, nearest_index(IFS, lat, lon))
    # Cascais: the column 9.5 W is column 1402 on GFS (350.5 E) and 682 on IFS.
    assert nearest_index(GFS, 38.70, -9.38) % 1440 == 1402
    assert nearest_index(IFS, 38.70, -9.38) % 1440 == 682


def test_icon_eu_grid():
    lat, lon = point_of(ICON, nearest_index(ICON, 38.70, -9.38))
    assert (lat, lon) == (38.6875, -9.375)
    assert contains(ICON, 38.70, -9.38) and contains(ICON, 29.5, -23.5) and contains(ICON, 70.5, 62.5)
    assert contains(ICON, 29.47, -23.53)  # within half a step of the corner
    assert not contains(ICON, 33.985, -118.4695)  # Venice Beach
    assert not contains(ICON, 28.0, -15.4)  # Gran Canaria: south of the domain
    assert not contains(ICON, 40.0, 63.0) and not contains(ICON, 40.0, -24.0)
    assert contains(GFS, -89.9, 359.9) and contains(IFS, 0.0, -180.0)  # a global grid holds everything


def test_decoded_fields_read_the_same_place_on_both_longitude_conventions():
    """A real GRIB2 written with ecCodes (only where it loads: Linux, the Docker image, CI)."""
    eccodes = pytest.importorskip("eccodes")
    np = pytest.importorskip("numpy")
    from raincheck_pipeline.grib import Sampler

    def message(lon_first, lon_last):
        gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        try:
            for k, v in [("Ni", 360), ("Nj", 181), ("latitudeOfFirstGridPointInDegrees", 90.0),
                         ("latitudeOfLastGridPointInDegrees", -90.0), ("longitudeOfFirstGridPointInDegrees", lon_first),
                         ("longitudeOfLastGridPointInDegrees", lon_last), ("iDirectionIncrementInDegrees", 1.0),
                         ("jDirectionIncrementInDegrees", 1.0)]:
                eccodes.codes_set(gid, k, v)
            # The value of each point is its longitude in -180..180, so a wrong column shows at once.
            lons = (lon_first + np.arange(360)) % 360.0
            lons = np.where(lons >= 180, lons - 360, lons)
            eccodes.codes_set_values(gid, np.tile(lons, 181).astype(float))
            return eccodes.codes_get_message(gid)
        finally:
            eccodes.codes_release(gid)

    points = [(38.70, -9.38), (33.985, -118.4695), (-33.8915, 151.2767)]
    for lon_first, lon_last in [(0.0, 359.0), (180.0, 179.0)]:
        _, values = Sampler(points).sample(message(lon_first, lon_last))
        assert values.tolist() == pytest.approx([-9.0, -118.0, 151.0], abs=0.01)
