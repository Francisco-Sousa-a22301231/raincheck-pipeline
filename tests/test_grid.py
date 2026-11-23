from raincheck_pipeline.grib import Grid, grid_point, land_index, nearest_index, point_of

# The IFS open-data grid: 0.25 degrees, north to south, starting at 180°W.
IFS = Grid(ni=1440, nj=721, lat_first=90.0, lon_first=180.0, d_lat=0.25, d_lon=0.25)


def test_nearest_index_corners():
    assert nearest_index(IFS, 90.0, 180.0) == 0
    assert nearest_index(IFS, 90.0, -180.0) == 0
    assert nearest_index(IFS, -90.0, 180.0) == 720 * 1440
    assert nearest_index(IFS, 90.0, 179.9) == 0  # wraps to the first column


def test_nearest_grid_point():
    assert grid_point(IFS, 38.6982, -9.3812) == (38.75, -9.5)
    assert grid_point(IFS, -33.87, 151.21) == (-33.75, 151.25)
    assert grid_point(IFS, 0.1, 0.1) == (0.0, 0.0)
    assert grid_point(IFS, 95.0, 0.0)[0] == 90.0  # latitudes are clamped


def test_grid_starting_at_greenwich():
    g = Grid(ni=1440, nj=721, lat_first=90.0, lon_first=0.0, d_lat=0.25, d_lon=0.25)
    assert grid_point(g, 38.6982, -9.3812) == (38.75, -9.5)
    assert nearest_index(g, 90.0, -0.1) == 0


def test_land_index_prefers_land_on_a_coast():
    sea = {nearest_index(IFS, 38.75, -9.5)}  # the plain nearest point of 38.6982, -9.3812
    idx = land_index(IFS, 38.6982, -9.3812, lambda i: i not in sea)
    assert point_of(IFS, idx) == (38.75, -9.25)  # the nearest of the other three
    # All four around it are land: the plain nearest one. None is: the plain nearest one too.
    assert land_index(IFS, 38.6982, -9.3812, lambda i: True) == nearest_index(IFS, 38.6982, -9.3812)
    assert land_index(IFS, 38.6982, -9.3812, lambda i: False) == nearest_index(IFS, 38.6982, -9.3812)


def test_land_index_across_the_date_line():
    only = nearest_index(IFS, -17.0, 180.0)
    assert point_of(IFS, land_index(IFS, -16.9, 179.9, lambda i: i == only)) == (-17.0, -180.0)
