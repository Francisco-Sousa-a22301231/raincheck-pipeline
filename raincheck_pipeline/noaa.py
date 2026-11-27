"""NOAA's GRIB files on AWS (GFS and GEFS): their `.idx` inventories and single-record downloads.

Every GRIB2 file there has a text inventory next to it, the same name ending in `.idx`, one line per
record (wgrib2's short inventory):

    597:430367565:d=2026112700:APCP:surface:0-9 hour acc fcst:
    n  :offset   :run date    :var :level  :time range        :(GEFS adds ENS=+30)

A record's length is the next record's offset minus its own (the last one runs to the end of the file),
so one record is one HTTP range request. Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .http import HttpError, request

# "0-9 hour acc fcst", "6-9 hour acc fcst", "0-1 day acc fcst", "0-3 day acc fcst" (GFS writes whole days
# in days: f024 is "0-1 day", f072 "0-3 day", but f030 "0-30 hour").
ACC_RE = re.compile(r"^(\d+)-(\d+) (hour|day) acc\b")
UNIT_H = {"hour": 1, "day": 24}


@dataclass(frozen=True)
class Record:
    number: int
    offset: int
    length: int | None  # None: the last record, to the end of the file
    var: str
    level: str
    when: str  # the time range text as written, e.g. "0-9 hour acc fcst"

    def acc_hours(self) -> tuple[int, int] | None:
        """(start, end) in hours after the run of an accumulation; None for anything else."""
        m = ACC_RE.match(self.when)
        if not m:
            return None
        unit = UNIT_H[m.group(3)]
        return int(m.group(1)) * unit, int(m.group(2)) * unit


def parse_idx(text: str) -> list[Record]:
    """Every record of an `.idx` inventory, in file order, with its length worked out."""
    rows: list[tuple[int, int, str, str, str]] = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        # "12.2" is the second field of message 12 (wgrib2 numbers fields that share one message so,
        # with the message's offset): they count as one message for the lengths.
        number = parts[0].split(".")[0]
        if len(parts) < 6 or not number.isdigit() or not parts[1].isdigit():
            continue
        rows.append((int(number), int(parts[1]), parts[3], parts[4], parts[5]))
    offsets = sorted({r[1] for r in rows})
    following = dict(zip(offsets, offsets[1:]))
    out: list[Record] = []
    for n, offset, var, level, when in sorted(rows, key=lambda r: (r[1], r[0])):
        nxt = following.get(offset)
        out.append(Record(n, offset, None if nxt is None else nxt - offset, var, level, when))
    return out


def rain_record(records: list[Record], step: int, run_total: bool) -> tuple[Record, int] | None:
    """The surface precipitation record (APCP) that ends at `step`, and the hour its accumulation starts.

    GFS has two at every step: the bucket that resets every 6 hours, and the run total (0 to `step`);
    with `run_total` the run total is taken (at f003 and f006 both say "0-3"/"0-6" and are the same
    numbers: the first one is fine). GEFS has only the bucket. None when the step has no such record.
    """
    found: list[tuple[int, Record]] = []
    for r in records:
        if r.var != "APCP" or r.level != "surface":
            continue
        hours = r.acc_hours()
        if hours is None or hours[1] != step:
            continue
        found.append((hours[0], r))
    if not found:
        return None
    if run_total:
        zero = [x for x in found if x[0] == 0]
        start, rec = zero[0] if zero else min(found, key=lambda x: x[0])
    else:
        start, rec = max(found, key=lambda x: x[0])  # the shortest bucket (GEFS has one anyway)
    return rec, start


def read_idx(url: str) -> list[Record] | None:
    """The inventory at `url` (ending in .idx); None when it is not there (404, or 403 as S3 answers for
    a key that does not exist in a bucket that cannot be listed). Other errors raise."""
    try:
        _, body = request(url)
    except HttpError as err:
        if err.status in (403, 404):
            return None
        raise
    return parse_idx(body.decode("utf-8", "replace"))


def exists(url: str) -> bool:
    """Whether a file is there (HEAD). 403/404 is "no"; other trouble raises (the caller cannot tell)."""
    try:
        request(url, method="HEAD")
        return True
    except HttpError as err:
        if err.status in (403, 404):
            return False
        raise


def all_exist(urls: list[str], threads: int = 8) -> bool:
    """Whether every file is there: the last one first (a run not out yet stops at one request), then the
    rest side by side. NOAA does not publish a run's steps strictly in order (27/11/2026, GFS 06 UTC at
    run + 4 h: f075 was out, f060 and f063 not yet), so the last step alone does not prove the others."""
    from concurrent.futures import ThreadPoolExecutor

    if not urls or not exists(urls[-1]):
        return False
    with ThreadPoolExecutor(max_workers=threads) as pool:
        return all(pool.map(exists, urls[:-1]))


def download(url: str, record: Record) -> bytes:
    """One GRIB record by range request, checked to start with GRIB and end with 7777."""
    end = "" if record.length is None else str(record.offset + record.length - 1)
    status, body = request(url, headers={"Range": f"bytes={record.offset}-{end}"})
    ok = status == 206 and body[:4] == b"GRIB" and body[-4:] == b"7777"
    if not ok or (record.length is not None and len(body) != record.length):
        raise ValueError(f"bad range answer for record {record.number} of {url} (status {status}, {len(body)} bytes)")
    return body
