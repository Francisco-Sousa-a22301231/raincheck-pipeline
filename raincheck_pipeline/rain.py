"""The rain-model job: GFS, GEFS, ICON-EU and AIFS rain per place, 72 h ahead, as `rain/v1/` tiles.

    python -m raincheck_pipeline rain [--check] [--cells] [--force] [--models gfs,gefs,...]
    python -m raincheck_pipeline rain --dry-run --points-file points.json --out out-rain

The contract with the Worker is docs/RAIN-TILES.md in its repository. In short: per 1° square one tile
`{ v, made, t, models: { <model>: { run, members? } }, zones: { <id>: { gfs, icon_eu, aifs, gefs_p,
gefs_mm } } }`, 24 three-hour steps from the step `made` falls in, mm rounded to 0.1, `gefs_p` in whole %.

The flow follows the base layer's (cli.py):
1. Every model on its own: the newest run whose last step the timeline needs is out (a HEAD request or
   two each), else the run before (each model has its own times: ICON-EU is out 3.3 h after its run,
   AIFS 6.1 h). A model that cannot be asked is left out of this job.
2. Every target's `GET /api/admin/rain/status` (one small stored object): it needs a build when any model
   has a newer run than the one it holds (`models`), or with --cells when its list of places changed.
   Only then is `GET /api/cells` read (a database read of every place on the server).
3. Download and sample model by model, in threads (`DOWNLOAD_THREADS`), one field at a time: a field
   (up to 8 MB decoded) is read at the places and dropped at once. A model that fails is logged and left
   out; the others still go.
4. Tiles, uploaded in batches (`PUT /api/admin/rain`, at most 60 tiles and 128 KB each), `last: true` on
   the final one.

Standard library only until step 3 (the `--check` on GitHub Actions runs without installing anything).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import aifs, gefs, gfs, icon_eu, rainmath, targets as tg
from .cli import log, set_output
from .ecmwf import SourceTrouble
from .http import counter
from .tiles import encode, tile_key

# The order the models are read in and written in a zone (the contract's example order).
MODELS = (gfs, icon_eu, aifs, gefs)
BY_NAME = {m.NAME: m for m in MODELS}
TILE_VERSION = 1


def run_name(run: int) -> str:
    return datetime.fromtimestamp(run, tz=timezone.utc).strftime("%Y-%m-%d %H UTC")


def steps_needed(mod, run: int, times: list[int]) -> list[int]:
    """The forecast steps (hours, ascending) of `run` that the timeline needs from `mod`."""
    return rainmath.needed_steps(mod.KIND, rainmath.leads(run, times), mod.MAX_STEP_H)


def newest_run(mod, now: int, times: list[int]) -> int | None:
    """The newest run of one model that has every step the timeline needs; None when none is out.

    Raises SourceTrouble when the model's server cannot say (the caller leaves the model out).
    """
    for run in mod.runs(now):
        steps = steps_needed(mod, run, times)
        if steps and mod.is_out(run, steps):
            return run
    return None


def newest_runs(now: int, times: list[int], models=MODELS) -> dict[str, int]:
    """{model: newest complete run} for every model that has one (each looked up on its own)."""
    found: dict[str, int] = {}
    for mod in models:
        try:
            run = newest_run(mod, now, times)
        except SourceTrouble as err:
            log(f"{mod.NAME}: could not be asked ({err}): left out of this job")
            continue
        if run is None:
            log(f"{mod.NAME}: no complete run in the last {mod.LOOK_BACK_H} h: left out of this job")
            continue
        found[mod.NAME] = run
        log(f"{mod.NAME}: run {run_name(run)} ({run})")
    return found


def held_runs(status: dict) -> dict[str, int]:
    """The run of each model a target's rain status says it holds (anything that is not a time is "none")."""
    models = status.get("models") if isinstance(status, dict) else None
    if not isinstance(models, dict):
        return {}
    return {k: int(v) for k, v in models.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def newer_models(status: dict, found: dict[str, int]) -> list[str]:
    """The models with a newer run than the target holds (or that it does not hold at all).

    A model the target holds a newer run of than found here (its server was briefly behind) gives no
    work: a build is never started to go back. If one is started for another model, the tiles carry the
    run found here for it (a whole tile is replaced at once).
    """
    held = held_runs(status)
    return [m for m, run in found.items() if m not in held or held[m] < run]


# ---------------------------------------------------------------------------------------------- download


def _scale(units: str) -> float:
    """Factor from a rain field's GRIB units to mm: IFS writes metres, the rest kg m-2 (= mm)."""
    u = units.strip()
    if u == "m":
        return 1000.0
    if u in ("kg m**-2", "kg m-2", "mm"):
        return 1.0
    raise ValueError(f"unexpected units for precipitation: {units!r}")


def extract_model(mod, run: int, times: list[int], points: list[tuple[float, float]], threads: int) -> dict:
    """Downloads and samples one model's run. Returns {"run", "series", "inside", "members"?, "sampler"}.

    `series[key]` is a list (one per step of `times`) of float arrays with a value per point (NaN: not
    known) or None (the run doesn't cover the step): key `gfs`, `icon_eu`, `aifs`, or `gefs_p` and
    `gefs_mm`. `inside` is a list of booleans (the point is inside the model's grid) or None (a global
    grid). Raises when the model cannot be built whole; for GEFS a member that fails is only left out.
    """
    import numpy as np

    from .grib import Sampler, contains

    leads = rainmath.leads(run, times)
    steps = rainmath.needed_steps(mod.KIND, leads, mod.MAX_STEP_H)
    sampler = Sampler(points)
    try:
        mask = mod.land(run)
    except Exception as err:  # without the mask a place reads its plain nearest point: still usable
        log(f"  {mod.NAME}: land-sea mask could not be read ({err})")
        mask = None
    if mask:
        sampler.set_land_mask(mask)
    else:
        log(f"  {mod.NAME}: no land-sea mask: every place reads its nearest grid point")

    members = mod.MEMBERS or [None]
    jobs = [(m, s) for m in members for s in steps]
    acc: dict[tuple[int | None, int], tuple[int, object]] = {}
    broken: set[int | None] = set()
    # At most two downloads per thread wait to be decoded: GEFS is 775 fields (220 MB), and with every
    # one submitted at once the finished ones piled up in memory faster than they were decoded.
    pool = ThreadPoolExecutor(max_workers=threads)
    pending = iter(jobs)
    futures: dict = {}

    def top_up() -> None:
        while len(futures) < 2 * threads:
            job = next(pending, None)
            if job is None:
                return
            futures[pool.submit(mod.fetch, run, job[1], job[0])] = job

    try:
        top_up()
        while futures:
            done = next(as_completed(list(futures)))
            m, s = futures.pop(done)
            top_up()
            try:
                got = done.result()
            except Exception as err:
                if mod.MEMBERS is None:
                    raise  # a deterministic model with a hole is not uploaded
                if m not in broken:
                    log(f"  {mod.NAME}: member {m} left out (step {s}: {err})")
                broken.add(m)
                continue
            if got is None:
                if mod.MEMBERS is None:
                    raise RuntimeError(f"step {s} of run {run_name(run)} is not on the server")
                broken.add(m)
                continue
            start, message = got
            name, values = sampler.sample(message)
            acc[(m, s)] = (start, values * np.float32(_scale(sampler.units.get(name, ""))))
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)

    inside = None
    if sampler.grid is not None and abs(sampler.grid.ni * sampler.grid.d_lon - 360.0) > 1e-6:
        inside = [contains(sampler.grid, la, lo) for la, lo in points]

    def series_of(member):
        get = lambda s: acc.get((member, s))  # noqa: E731
        if mod.KIND == "six_hourly":
            return rainmath.six_hourly_halves(leads, get, mod.MAX_STEP_H)
        return rainmath.three_hourly(leads, get, mod.MAX_STEP_H)

    out: dict = {"run": run, "inside": inside, "sampler": sampler}
    if mod.MEMBERS is None:
        out["series"] = {mod.NAME: series_of(None)}
        return out
    whole = [m for m in members if m not in broken]
    if len(whole) < rainmath.MIN_MEMBERS:
        raise RuntimeError(f"only {len(whole)} of {len(members)} members could be read")
    per_member = [series_of(m) for m in whole]
    acc.clear()
    chance, median = rainmath.ensemble(per_member, len(points))
    covered = [any(s[k] is not None for s in per_member) for k in range(len(times))]
    out["series"] = {
        f"{mod.NAME}_p": [chance[k] if covered[k] else None for k in range(len(times))],
        f"{mod.NAME}_mm": [median[k] if covered[k] else None for k in range(len(times))],
    }
    out["members"] = len(whole)
    return out


def extract_all(found: dict[str, int], times: list[int], points: list[tuple[float, float]], threads: int) -> dict[str, dict]:
    """Every model found, one after the other; a model that fails is logged and left out."""
    results: dict[str, dict] = {}
    for mod in MODELS:
        if mod.NAME not in found:
            continue
        b0, r0, t0 = counter.bytes, counter.requests, time.monotonic()
        try:
            results[mod.NAME] = extract_model(mod, found[mod.NAME], times, points, threads)
        except Exception as err:
            log(f"{mod.NAME}: LEFT OUT ({err})")
            continue
        extra = f", {results[mod.NAME]['members']} members" if "members" in results[mod.NAME] else ""
        log(f"{mod.NAME}: run {run_name(found[mod.NAME])} read{extra} ({(counter.bytes - b0) / 1e6:.1f} MB in "
            f"{counter.requests - r0} requests, {time.monotonic() - t0:.0f} s)")
    return results


# ------------------------------------------------------------------------------------------------- tiles


def build_rain_tiles(cells: list[dict], where: dict[tuple[float, float], int], made: int, times: list[int],
                     results: dict[str, dict]) -> dict[str, dict]:
    """Groups every place's series into 1° tiles (the contract's shape): `{ key: tile }`.

    `results[model]` as extract_model returns it (only "run", "members", "series" and "inside" are read:
    any indexable values work). A model missing from `results` is absent from `models` and from every
    zone; a place outside a regional model's grid has no key for it.
    """
    meta: dict[str, dict] = {}
    for name in ("gfs", "gefs", "icon_eu", "aifs"):
        if name in results:
            meta[name] = {"run": results[name]["run"]}
            if "members" in results[name]:
                meta[name]["members"] = results[name]["members"]
    order = [m.NAME for m in MODELS if m.NAME in results]
    tiles: dict[str, dict] = {}
    for cell in cells:
        i = where[(cell["lat"], cell["lon"])]
        zone: dict[str, list] = {}
        for name in order:
            r = results[name]
            if r.get("inside") is not None and not r["inside"][i]:
                continue
            for key, steps in r["series"].items():
                fmt = rainmath.percent if key.endswith("_p") else rainmath.mm
                zone[key] = [None if s is None else fmt(s[i]) for s in steps]
        key = tile_key(cell["lat"], cell["lon"])
        tile = tiles.setdefault(key, {"v": TILE_VERSION, "made": made, "t": times, "models": meta, "zones": {}})
        tile["zones"][cell["id"]] = zone
    return tiles


# --------------------------------------------------------------------------------------------- command


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raincheck_pipeline rain", description="GFS, GEFS, ICON-EU and AIFS rain tiles (rain/v1).")
    ap.add_argument("--check", action="store_true", help="only say whether any target needs a newer run of a model (no download)")
    ap.add_argument("--cells", action="store_true", help="also compare the list of places of a target that is up to date")
    ap.add_argument("--dry-run", action="store_true", help="no server involved: places from --points-file, tiles written to --out")
    ap.add_argument("--points-file", help='JSON list of {"id", "lat", "lon"} (dry run)')
    ap.add_argument("--out", default="out-rain", help="folder for the dry run's tiles (default: out-rain)")
    ap.add_argument("--force", action="store_true", help="build and upload even when every target is up to date")
    ap.add_argument("--models", help=f"only these models, comma-separated (default: all of {','.join(BY_NAME)})")
    args = ap.parse_args(argv)

    started = time.monotonic()
    threads = int(os.environ.get("DOWNLOAD_THREADS", "8"))
    max_tiles = min(300, int(os.environ.get("BATCH_TILES", "60")))
    max_bytes = int(os.environ.get("BATCH_BYTES", str(128 * 1024)))
    if args.dry_run and not args.points_file:
        ap.error("--dry-run needs --points-file")
    models = MODELS
    if args.models:
        names = [n.strip() for n in args.models.split(",") if n.strip()]
        unknown = [n for n in names if n not in BY_NAME]
        if unknown:
            ap.error(f"unknown model(s): {', '.join(unknown)}")
        models = tuple(m for m in MODELS if m.NAME in names)

    # 1. The targets (dry run: none).
    usable: list[tg.Target] = []
    if not args.dry_run:
        usable, notes = tg.from_env(os.environ)
        for note in notes:
            log(note)
        if not usable:
            log("no usable target (set TARGETS and, for each name, <NAME>_URL and <NAME>_TOKEN): nothing to do")
            set_output("needed", "false")
            return 0

    # 2. The timeline of this job and every model's newest run that covers it.
    made = int(time.time())
    times = rainmath.timeline(made)
    found = newest_runs(made, times, models)
    if not found:
        log("no model has a complete run: nothing to do")
        set_output("needed", "false")
        return 1

    # 3. Who needs what.
    work: list[tuple[tg.Target | None, list[dict], str]] = []
    failed = False
    if args.dry_run:
        cells, dropped = tg.valid_cells(json.loads(Path(args.points_file).read_text(encoding="utf-8")))
        if dropped:
            log(f"{dropped} point(s) in {args.points_file} were not usable and are left out")
        work.append((None, cells, tg.cells_signature(cells)))
    wanted: list[str] = []
    for target in usable:
        try:
            status = tg.get_status(target, tg.RAIN_STATUS)
            newer = newer_models(status, found)
            if args.check and (args.force or newer):
                log(f"target {target.name}: needs a build ({'newer ' + ', '.join(newer) if newer else 'forced'})")
                wanted.append(target.name)
                continue
            cells = None
            if args.force or newer or args.cells:
                cells = tg.get_cells(target)
                signature = tg.cells_signature(cells)
        except Exception as err:  # one target down must not stop the others
            log(f"target {target.name}: could not be asked ({err})")
            failed = True
            continue
        if cells is None:
            log(f"target {target.name}: up to date (every model's newest run held)")
        elif newer:
            log(f"target {target.name}: needs a build ({len(cells)} places; newer {', '.join(newer)})")
            work.append((target, cells, signature))
        elif status.get("cells") != signature:
            log(f"target {target.name}: needs a build ({len(cells)} places; its list of places changed)")
            work.append((target, cells, signature))
        elif args.force:
            log(f"target {target.name}: gets a build again ({len(cells)} places; forced)")
            work.append((target, cells, signature))
        else:
            log(f"target {target.name}: up to date (same runs, same list of places)")

    if args.check:
        needed = bool(work or wanted)
        set_output("needed", "true" if needed else "false")
        log("work to do" if needed else "nothing to do")
        return 1 if failed and not needed else 0
    if not work:
        log("nothing to do")
        return 1 if failed else 0

    # 4. Download and sample once for every place of every target that needs a build.
    points = sorted({(c["lat"], c["lon"]) for _, cells, _ in work for c in cells})
    where = {p: i for i, p in enumerate(points)}
    results = extract_all(found, times, points, threads) if points else {}
    if points and not results:
        log("no model could be read: nothing is uploaded (the targets keep what they hold)")
        return 1
    if args.dry_run:
        from .grib import point_of

        for name, r in results.items():
            sampler = r["sampler"]
            if sampler.grid is None:
                continue
            for c in work[0][1][:20]:
                i = where[(c["lat"], c["lon"])]
                if r["inside"] is not None and not r["inside"][i]:
                    log(f"  {name:8} {c['id']}: outside the model's grid")
                    continue
                glat, glon = point_of(sampler.grid, sampler.index_of(sampler.grid, c["lat"], c["lon"]))
                log(f"  {name:8} {c['id']}: {c['lat']}, {c['lon']} -> grid point {glat:.4f}, {glon:.4f}")

    # 5. Tiles, per target.
    held = {name: r["run"] for name, r in results.items()}
    for target, cells, signature in work:
        tiles = build_rain_tiles(cells, where, made, times, results)
        size = sum(len(encode(t)) for t in tiles.values())
        if target is None:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            for key, tile in tiles.items():
                (out / f"{key}.json").write_bytes(encode(tile))
            log(f"dry run: {len(tiles)} tiles ({size} bytes) for {len(cells)} places written to {out}")
            continue
        try:
            n = tg.upload_rain(target, tiles, signature, held, max_tiles, max_bytes)
            log(f"target {target.name}: {len(tiles)} tiles ({size} bytes) for {len(cells)} places uploaded in {n} request(s)")
        except Exception as err:
            log(f"target {target.name}: UPLOAD FAILED: {err}")
            failed = True

    log(f"done in {time.monotonic() - started:.0f} s, {counter.bytes / 1e6:.1f} MB downloaded in {counter.requests} requests")
    return 1 if failed else 0
