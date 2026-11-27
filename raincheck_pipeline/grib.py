"""GRIB decoding (ecCodes) and sampling at points. The only module that needs eccodes and numpy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Grid:
    """A regular latitude/longitude grid as GRIB describes it."""

    ni: int  # points along a parallel
    nj: int  # points along a meridian
    lat_first: float
    lon_first: float
    d_lat: float  # positive
    d_lon: float  # positive
    north_to_south: bool = True  # rows run from lat_first southwards
    west_to_east: bool = True


def nearest_index(grid: Grid, lat: float, lon: float) -> int:
    """Index (in the flat values array) of the grid point nearest to a place.

    No interpolation. At 0.25 degrees the point is at most about 14 km away in latitude. Longitudes
    wrap; latitudes are clamped to the grid.
    """
    row = (grid.lat_first - lat) / grid.d_lat if grid.north_to_south else (lat - grid.lat_first) / grid.d_lat
    j = min(grid.nj - 1, max(0, int(round(row))))
    east = (lon - grid.lon_first) % 360.0 if grid.west_to_east else (grid.lon_first - lon) % 360.0
    i = int(round(east / grid.d_lon))
    if i >= grid.ni:
        # Past the last column: a global grid wraps to the first one; a regional grid ends there.
        i = 0 if abs(grid.ni * grid.d_lon - 360.0) < 1e-6 else grid.ni - 1
    return j * grid.ni + i


def land_index(grid: Grid, lat: float, lon: float, is_land: Callable[[int], bool]) -> int:
    """Like `nearest_index`, but prefers land.

    The nearest of the four grid points around the place that is land; when none is (open sea, a small
    island) the plain nearest one. On a coast the nearest point is often sea, where the model has far
    stronger wind and milder air than the ground next to it.
    """
    row = (grid.lat_first - lat) / grid.d_lat if grid.north_to_south else (lat - grid.lat_first) / grid.d_lat
    col = ((lon - grid.lon_first) % 360.0 if grid.west_to_east else (grid.lon_first - lon) % 360.0) / grid.d_lon
    wraps = abs(grid.ni * grid.d_lon - 360.0) < 1e-6
    best: tuple[float, int] | None = None
    for j in (math.floor(row), math.floor(row) + 1):
        if not 0 <= j < grid.nj:
            continue
        for i in (math.floor(col), math.floor(col) + 1):
            if i >= grid.ni and not wraps:
                continue
            idx = j * grid.ni + i % grid.ni
            if not is_land(idx):
                continue
            # Squared distance in degrees of latitude (a degree of longitude shrinks with the latitude).
            d = ((row - j) * grid.d_lat) ** 2 + ((col - i) * grid.d_lon * math.cos(math.radians(lat))) ** 2
            if best is None or d < best[0]:
                best = (d, idx)
    return best[1] if best else nearest_index(grid, lat, lon)


def contains(grid: Grid, lat: float, lon: float) -> bool:
    """Whether a place is inside the grid (with half a grid step around its edge).

    Only matters for regional grids (ICON-EU): `nearest_index` clamps a place outside them to the edge,
    which would hand a place in Brazil the rain of the Azores. A global grid contains everything.
    """
    if grid.north_to_south:
        south, north = grid.lat_first - (grid.nj - 1) * grid.d_lat, grid.lat_first
    else:
        south, north = grid.lat_first, grid.lat_first + (grid.nj - 1) * grid.d_lat
    if not south - grid.d_lat / 2 <= lat <= north + grid.d_lat / 2:
        return False
    if abs(grid.ni * grid.d_lon - 360.0) < 1e-6:
        return True
    east = (lon - grid.lon_first) % 360.0 if grid.west_to_east else (grid.lon_first - lon) % 360.0
    span = (grid.ni - 1) * grid.d_lon
    # East of the last column by up to half a step, or west of the first by up to half a step (which
    # the modulo turns into "almost 360 degrees east").
    return east <= span + grid.d_lon / 2 or east >= 360.0 - grid.d_lon / 2


def point_of(grid: Grid, idx: int) -> tuple[float, float]:
    """The coordinates of a grid point from its index (longitude in -180..180)."""
    j, i = divmod(idx, grid.ni)
    glat = grid.lat_first - j * grid.d_lat if grid.north_to_south else grid.lat_first + j * grid.d_lat
    glon = grid.lon_first + i * grid.d_lon if grid.west_to_east else grid.lon_first - i * grid.d_lon
    return glat, ((glon + 180.0) % 360.0) - 180.0


def grid_point(grid: Grid, lat: float, lon: float) -> tuple[float, float]:
    """The coordinates of the grid point `nearest_index` picks for a place."""
    return point_of(grid, nearest_index(grid, lat, lon))


LAND_SHARE = 0.5  # a grid box counts as land from this share of land (the `lsm` field)


def _grid_of(eccodes, gid) -> Grid:
    grid_type = eccodes.codes_get(gid, "gridType")
    if grid_type != "regular_ll":
        raise ValueError(f"unexpected grid type {grid_type}")
    return Grid(
        ni=eccodes.codes_get(gid, "Ni"),
        nj=eccodes.codes_get(gid, "Nj"),
        lat_first=eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees"),
        lon_first=eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees"),
        d_lat=eccodes.codes_get(gid, "jDirectionIncrementInDegrees"),
        d_lon=eccodes.codes_get(gid, "iDirectionIncrementInDegrees"),
        north_to_south=eccodes.codes_get(gid, "jScansPositively") == 0,
        west_to_east=eccodes.codes_get(gid, "iScansNegatively") == 0,
    )


class Sampler:
    """Decodes GRIB messages and reads them at a fixed list of points."""

    def __init__(self, points: list[tuple[float, float]]):
        import numpy  # imported here so the rest of the package runs without it

        self._np = numpy
        self._points = points
        self._index_cache: dict[Grid, object] = {}
        self._land: tuple[Grid, object] | None = None
        self.cloud_scale: float | None = None  # 1 when tcc is in %, 100 when it is 0..1
        self.grid: Grid | None = None
        self.units: dict[str, str] = {}  # short name -> GRIB units, as read (tp: "m" for IFS, "kg m**-2" elsewhere)

    @property
    def size(self) -> int:
        """How many points are read."""
        return len(self._points)

    def set_land_mask(self, message: bytes) -> None:
        """The land-sea mask of the model (`lsm`): from now on places on a coast read the land side."""
        import eccodes

        gid = eccodes.codes_new_from_message(message)
        try:
            self._land = (_grid_of(eccodes, gid), eccodes.codes_get_values(gid) >= LAND_SHARE)
        finally:
            eccodes.codes_release(gid)
        self._index_cache.clear()

    def index_of(self, grid: Grid, lat: float, lon: float) -> int:
        if self._land and self._land[0] == grid:
            mask = self._land[1]
            return land_index(grid, lat, lon, lambda idx: bool(mask[idx]))
        return nearest_index(grid, lat, lon)

    def _indices(self, grid: Grid):
        if grid not in self._index_cache:
            np = self._np
            self._index_cache[grid] = np.array([self.index_of(grid, la, lo) for la, lo in self._points], dtype=np.int64)
        return self._index_cache[grid]

    def sample(self, message: bytes):
        """(short name, float32 array with the value at each point; NaN where the field has no value)."""
        import eccodes

        np = self._np
        gid = eccodes.codes_new_from_message(message)
        try:
            grid = _grid_of(eccodes, gid)
            name = eccodes.codes_get(gid, "shortName")
            self.units[name] = str(eccodes.codes_get(gid, "units"))
            if name == "tcc" and self.cloud_scale is None:
                self.cloud_scale = 1.0 if "%" in self.units[name] else 100.0
            missing = eccodes.codes_get(gid, "missingValue")
            has_bitmap = eccodes.codes_get(gid, "bitmapPresent") == 1
            values = eccodes.codes_get_values(gid)
        finally:
            eccodes.codes_release(gid)
        if values.size != grid.ni * grid.nj:
            raise ValueError(f"{name}: {values.size} values for a {grid.ni}x{grid.nj} grid")
        self.grid = grid
        picked = values[self._indices(grid)]  # a copy: the full field (8 MB) is dropped here
        if has_bitmap:
            picked[picked == missing] = np.nan
        return name, picked.astype(np.float32)
