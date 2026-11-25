"""Command line: check for a new run, build the tiles, upload them (or write them to a folder)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import ecmwf, ens, targets as tg
from .http import counter
from .tiles import (EARLIER_STEPS, HOUR, RUN_EVERY_H, SPAN_H, build_tiles, chance_on_timeline, earlier_runs, encode,
                    future_steps, holes, point_series)


def log(message: str) -> None:
    print(message, flush=True)


def parse_run(text: str) -> int:
    return int(datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp())


def set_output(name: str, value: str) -> None:
    """A step output for GitHub Actions (nothing outside it)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def newest_run(now: int, with_ens: bool = True) -> tuple[int | None, bool]:
    """(the newest run to build, whether its ENS rain chance is out); (None, False) when no run is complete.

    A run is complete when its HRES 72 h step has every field (ecmwf.latest_run's rule). Its ENS comes out
    minutes later: until ens.WAIT_H hours after the run's time a complete run without its ENS is passed
    over for the run before it (which a target normally holds already: nothing to do, the next check
    tries again); after that it is built without the rain chance. Raises ecmwf.SourceTrouble like
    latest_run when ECMWF cannot be asked about HRES.
    """
    newest = now - now % (RUN_EVERY_H * HOUR)
    for back in range(0, ecmwf.LOOK_BACK_H + 1, RUN_EVERY_H):
        run = newest - back * HOUR
        fields = ecmwf.step_fields(run, SPAN_H, ask_all=False)
        if fields is None or not all(p in fields for p in ecmwf.PARAMS):
            continue
        if not with_ens:
            return run, False
        if ens.is_out(run):
            return run, True
        if now - run >= ens.WAIT_H * HOUR:
            log(f"run {ecmwf.run_name(run)}: no ENS {ens.WAIT_H} h after the run: built without the rain chance")
            return run, False
        log(f"run {ecmwf.run_name(run)}: HRES is out, its ENS not yet: waiting for it (at most {ens.WAIT_H} h after the run)")
    return None, False


def fetch_step(job: tuple[int, int]) -> tuple[tuple[int, int], dict[str, bytes] | None]:
    """Downloads the seven fields of one (run, step); None when the sources say the step is not there (404).

    Raises when no source could be asked, or when a field cannot be downloaded.
    """
    run, step = job
    fields = ecmwf.step_fields(run, step)
    if fields is None:
        return job, None
    return job, {p: ecmwf.download(f) for p, f in fields.items()}


def extract(run: int, points: list[tuple[float, float]], threads: int):
    """Downloads and samples every needed field. Returns (values, sampler).

    values[(run, step)][param] is a float32 array with the value at each point (NaN where missing):
    4 bytes per point and field, so 30,000 points stay near 40 MB.
    """
    from .grib import Sampler  # needs numpy and eccodes: only loaded when there is work

    sampler = Sampler(points)
    # The land-sea mask first (one small field of the run's step 0): coastal places read the land side.
    mask = (ecmwf.step_fields(run, 0, params=("lsm",)) or {}).get("lsm")
    if mask:
        sampler.set_land_mask(ecmwf.download(mask))
    else:
        log("  no land-sea mask in the run: every place reads its nearest grid point, sea or not")
    jobs = [(run, s) for s in future_steps()] + [(r, s) for r in earlier_runs(run) for s in EARLIER_STEPS]
    values: dict[tuple[int, int], dict[str, list]] = {}
    gone: list[tuple[int, int]] = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for done in as_completed([pool.submit(fetch_step, j) for j in jobs]):
            job, messages = done.result()
            if messages is None:
                gone.append(job)
                continue
            values[job] = dict(sampler.sample(m) for m in messages.values())
    lacking = holes(run, {job: set(fields) for job, fields in values.items()})
    if lacking:
        raise RuntimeError(f"run {ecmwf.run_name(run)} is not whole on any source ({'; '.join(lacking)}): nothing is uploaded")
    for r in sorted({r for r, _ in gone}):
        log(f"  earlier run {ecmwf.run_name(r)} is no longer served (404): its hours stay empty (null)")
    return values, sampler


def extract_ens(run: int, sampler, threads: int):
    """The ENS rain chance of the run: float array [len(ens.steps()), points] (%, NaN: not known).

    Downloads in threads, samples here, one field at a time (about 12 ms each). Raises when a step or a
    member cannot be read; the caller then leaves the chance out rather than failing the run.
    """
    import numpy as np

    steps = ens.steps()
    with ThreadPoolExecutor(max_workers=threads) as pool:
        found = dict(zip(steps, pool.map(lambda s: ens.step_members(run, s), steps)))
    for s, fields in found.items():
        if not fields or len(fields) < ens.MIN_MEMBERS:
            raise RuntimeError(f"ENS step {s}: {len(fields or [])} members on the sources")
    names = sorted({f.param for fields in found.values() for f in fields})
    row = {name: k for k, name in enumerate(names)}
    acc = [np.full((len(names), sampler.size), np.nan, dtype=np.float32) for _ in steps]
    at = {s: k for k, s in enumerate(steps)}

    def fetch(job):
        step, field = job
        return step, field.param, ecmwf.download(field)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for step, name, message in pool.map(fetch, [(s, f) for s in steps for f in found[s]]):
            acc[at[step]][row[name]] = sampler.sample(message)[1]
    return ens.chance(acc)


def series_for(run: int, cells: list[dict], values: dict, where: dict[tuple[float, float], int], cloud_scale: float,
               chance=None) -> list[dict]:
    """The tile series of every cell; with `chance` (extract_ens) each also gets `p`, the ENS rain chance."""
    out = []
    steps = ens.steps()
    for cell in cells:
        i = where[(cell["lat"], cell["lon"])]

        def get(r: int, s: int, p: str, i: int = i):
            column = values.get((r, s), {}).get(p)
            if column is None:
                return None
            v = float(column[i])
            return None if v != v else v  # NaN: no value there

        series = point_series(run, cell["lat"], cell["lon"], get, cloud_scale)
        if chance is not None:
            series["p"] = chance_on_timeline(run, {s: float(chance[k][i]) for k, s in enumerate(steps)})
        out.append(series)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raincheck_pipeline", description=__doc__)
    ap.add_argument("--check", action="store_true", help="only say whether any target needs the newest run (no download)")
    ap.add_argument("--cells", action="store_true",
                    help="also compare the list of points of a target that already holds the run (the server reads "
                         "all its places for that: not for every check)")
    ap.add_argument("--dry-run", action="store_true", help="no server involved: points from --points-file, tiles written to --out")
    ap.add_argument("--points-file", help='JSON list of {"id", "lat", "lon"} (dry run)')
    ap.add_argument("--out", default="out", help="folder for the dry run's tiles (default: out)")
    ap.add_argument("--run", help="use this run (YYYYMMDDHH, UTC) instead of the newest complete one")
    ap.add_argument("--force", action="store_true", help="upload even to a target that already holds the run")
    ap.add_argument("--no-ens", action="store_true", help="leave the ENS rain chance out (no wait for it, no download)")
    args = ap.parse_args(argv)

    started = time.monotonic()
    threads = int(os.environ.get("DOWNLOAD_THREADS", "8"))
    # Small batches: the server (Workers Free) has about 10 ms of processor time per request, and parsing,
    # checking and storing 300 tiles (0.6 MB) took 6.4 ms on a fast desktop. 60 tiles is about 1.3 ms there.
    max_tiles = min(300, int(os.environ.get("BATCH_TILES", "60")))
    max_bytes = int(os.environ.get("BATCH_BYTES", str(128 * 1024)))

    if args.dry_run and not args.points_file:
        ap.error("--dry-run needs --points-file")

    # 1. The targets (dry run: none). Without any there is nothing to ask ECMWF for.
    usable: list[tg.Target] = []
    if not args.dry_run:
        usable, notes = tg.from_env(os.environ)
        for note in notes:
            log(note)
        if not usable:
            log("no usable target (set TARGETS and, for each name, <NAME>_URL and <NAME>_TOKEN): nothing to do")
            set_output("needed", "false")
            return 0

    # 2. The newest complete run (one or two small requests).
    try:
        if args.run:
            run = parse_run(args.run)
            with_ens = not args.no_ens and ens.is_out(run)
        else:
            run, with_ens = newest_run(int(time.time()), with_ens=not args.no_ens)
    except ecmwf.SourceTrouble as err:
        log(f"ECMWF could not be asked: {err}")
        return 1
    if run is None:
        log("no complete IFS run found on any source")
        return 1
    log(f"newest complete run: {ecmwf.run_name(run)} ({run})" + ("" if with_ens else ", without the ENS rain chance"))

    # 3. Who needs it, and for which points.
    work: list[tuple[tg.Target | None, list[dict], str]] = []
    failed = False
    if args.dry_run:
        cells, dropped = tg.valid_cells(json.loads(Path(args.points_file).read_text(encoding="utf-8")))
        if dropped:
            log(f"{dropped} point(s) in {args.points_file} were not usable and are left out")
        work.append((None, cells, tg.cells_signature(cells)))
    wanted: list[str] = []  # --check: targets that need the run whatever their points are
    for target in usable:
        cells: list[dict] | None = None
        try:
            # The status first: it costs the server nothing. Its list of points (a database read of
            # every place) is only asked for when there is a run to build for it, or with --cells.
            status = tg.get_status(target)
            look = tg.first_look(status, run)
            held = f"holds run {status.get('run')}"
            if args.check and (args.force or look == "older"):
                log(f"target {target.name}: needs the run ({held if look == 'older' else 'forced'})")
                wanted.append(target.name)
                continue
            if args.force or look == "older" or (look == "same" and args.cells):
                cells = tg.get_cells(target)
                signature = tg.cells_signature(cells)
        except Exception as err:  # one target down must not stop the others
            log(f"target {target.name}: could not be asked ({err})")
            failed = True
            continue
        if cells is None:
            note = "holds a newer run" if look == "newer" else "holds the run; its list of points is only compared with --cells"
            log(f"target {target.name}: up to date ({note})")
        elif look == "older" or tg.needs(status, run, signature):
            why = held if look == "older" else "its list of points changed"
            log(f"target {target.name}: needs the run ({len(cells)} points; {why})")
            work.append((target, cells, signature))
        elif args.force:
            log(f"target {target.name}: gets the run again ({len(cells)} points; forced)")
            work.append((target, cells, signature))
        else:
            log(f"target {target.name}: up to date (same run, same list of points)")

    if args.check:
        needed = bool(work or wanted)
        set_output("needed", "true" if needed else "false")
        log("work to do" if needed else "nothing to do")
        return 1 if failed and not needed else 0
    if not work:
        log("nothing to do")
        return 1 if failed else 0

    # 4. Download and sample once for every point of every target that needs the run.
    points = sorted({(c["lat"], c["lon"]) for _, cells, _ in work for c in cells})
    where = {p: i for i, p in enumerate(points)}
    values: dict = {}
    cloud_scale = 1.0
    chance = None
    if points:
        values, sampler = extract(run, points, threads)
        cloud_scale = sampler.cloud_scale or 1.0
        fields = sum(len(v) for v in values.values())
        log(f"sampled {fields} fields of {len(values)} steps at {len(points)} points "
            f"({counter.bytes / 1e6:.1f} MB in {counter.requests} requests, {time.monotonic() - started:.0f} s)")
        if with_ens:
            b0, r0, t0 = counter.bytes, counter.requests, time.monotonic()
            try:
                chance = extract_ens(run, sampler, threads)
                log(f"ENS rain chance: {len(ens.steps())} steps ({(counter.bytes - b0) / 1e6:.1f} MB in "
                    f"{counter.requests - r0} requests, {time.monotonic() - t0:.0f} s)")
            except Exception as err:  # the base layer without the chance is better than none
                log(f"  warning: ENS rain chance left out ({err})")
        if args.dry_run and sampler.grid:
            from .grib import point_of

            for c in work[0][1][:20]:
                glat, glon = point_of(sampler.grid, sampler.index_of(sampler.grid, c["lat"], c["lon"]))
                log(f"  {c['id']}: {c['lat']}, {c['lon']} -> grid point {glat:.2f}, {glon:.2f}")

    # 5. Tiles, per target.
    made = int(time.time())
    for target, cells, signature in work:
        tiles = build_tiles(cells, series_for(run, cells, values, where, cloud_scale, chance), run, made)
        size = sum(len(encode(t)) for t in tiles.values())
        if target is None:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            for key, tile in tiles.items():
                (out / f"{key}.json").write_bytes(encode(tile))
            log(f"dry run: {len(tiles)} tiles ({size} bytes) for {len(cells)} points written to {out}")
            continue
        try:
            n = tg.upload(target, tiles, run, signature, max_tiles, max_bytes)
            log(f"target {target.name}: {len(tiles)} tiles ({size} bytes) for {len(cells)} points uploaded in {n} request(s)")
        except Exception as err:
            log(f"target {target.name}: UPLOAD FAILED: {err}")
            failed = True

    log(f"done in {time.monotonic() - started:.0f} s, {counter.bytes / 1e6:.1f} MB downloaded in {counter.requests} requests")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
