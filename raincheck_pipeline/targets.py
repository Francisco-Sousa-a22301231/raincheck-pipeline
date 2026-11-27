"""The servers the tiles go to: which points they want, which run they hold, and the upload.

Targets come from the environment: `TARGETS` is a comma-separated list of names; each name has
`<NAME>_URL` and `<NAME>_TOKEN`. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from .http import HttpError, request
from .tiles import batches, encode

USER_AGENT = "raincheck-pipeline/1.0"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Plain http is only accepted for these (a server on the same machine; the last one is that machine seen
# from inside a Docker container).
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    token: str


def env_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", name.strip().upper())


def from_env(env: Mapping[str, str]) -> tuple[list[Target], list[str]]:
    """(usable targets, messages about the ones skipped)."""
    targets: list[Target] = []
    notes: list[str] = []
    for raw in (env.get("TARGETS") or "").split(","):
        if not raw.strip():
            continue
        name = env_name(raw)
        url = (env.get(f"{name}_URL") or "").strip().rstrip("/")
        token = (env.get(f"{name}_TOKEN") or "").strip()
        if not url:
            notes.append(f"target {name}: skipped, {name}_URL is not set")
            continue
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            notes.append(f"target {name}: skipped, {name}_URL is not an http(s) address")
            continue
        if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
            notes.append(f"target {name}: skipped, {name}_URL must be https (the token would travel in clear)")
            continue
        if not token:
            notes.append(f"target {name}: skipped, {name}_TOKEN is not set")
            continue
        targets.append(Target(name, url, token))
    return targets, notes


def valid_cells(raw: object) -> tuple[list[dict], int]:
    """(the usable points as {id, lat, lon}, how many were dropped). Accepts a list or {"cells": [...]}."""
    rows = raw.get("cells") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("expected a list of points or {\"cells\": [...]}")
    out: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for row in rows:
        try:
            cid, lat, lon = row["id"], float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        ok = isinstance(cid, str) and ID_RE.match(cid) and cid not in seen
        if not ok or not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            dropped += 1
            continue
        seen.add(cid)
        out.append({"id": cid, "lat": lat, "lon": lon})
    return out, dropped


def cells_signature(cells: list[dict]) -> str:
    """A short fingerprint of the list of points: a server whose list changed gets its tiles rebuilt."""
    lines = sorted(f"{c['id']},{c['lat']:.5f},{c['lon']:.5f}" for c in cells)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def get_cells(target: Target) -> list[dict]:
    """The server's list of points (it reads all its places for this: ask only when there is a use for it)."""
    _, body = request(
        f"{target.url}/api/cells",
        user_agent=USER_AGENT,
        headers={"Authorization": f"Bearer {target.token}", "Accept": "application/json"},
    )
    cells, dropped = valid_cells(json.loads(body))
    if dropped:
        print(f"  {dropped} point(s) from {target.url}/api/cells were not usable and are left out", flush=True)
    return cells


BASE_STATUS = "/api/admin/base/status"
RAIN_STATUS = "/api/admin/rain/status"


def get_status(target: Target, path: str = BASE_STATUS) -> dict:
    """What the server holds. Base layer: {"run": <unix s or None>, "cells": <signature or None>, ...};
    rain models (`path=RAIN_STATUS`): {"made", "cells", "models": {<model>: <run>}, ...}."""
    _, body = request(
        f"{target.url}{path}",
        user_agent=USER_AGENT,
        headers={"Authorization": f"Bearer {target.token}", "Accept": "application/json"},
    )
    status = json.loads(body)
    return status if isinstance(status, dict) else {}


def first_look(status: dict, run: int) -> str:
    """What the status alone says about `run`, before anyone asks for the list of points.

    "older": the server holds an older run or none, so it needs this one whatever its points are;
    "same": it holds this run, and only a changed list of points could give work; "newer": it holds a
    newer one (never go back). The status costs the server one small stored object; its list of points
    costs it a database read of every place, so that is only asked for when this says it is worth it.
    """
    stored = status.get("run")
    if isinstance(stored, bool) or not isinstance(stored, (int, float)):
        return "older"
    return "older" if stored < run else "same" if stored == run else "newer"


def needs(status: dict, run: int, signature: str) -> bool:
    """True when the server should get this run: it holds an older one, or its list of points changed."""
    stored = status.get("run")
    if not isinstance(stored, (int, float)):
        return True
    if stored > run:
        return False  # never go back to an older run
    return stored < run or status.get("cells") != signature


def upload(target: Target, tiles: dict[str, dict], run: int, signature: str, max_tiles: int, max_bytes: int) -> int:
    """PUTs the tiles in batches; the last one is marked so the server records the run as complete.

    Raises on any answer that is not 2xx (after retries). Returns the number of requests made.
    """
    return put_batches(target, "/api/admin/base", tiles, {"run": run, "cells": signature}, max_tiles, max_bytes)


def upload_rain(target: Target, tiles: dict[str, dict], signature: str, models: dict[str, int],
                max_tiles: int, max_bytes: int) -> int:
    """The rain-model tiles (docs/RAIN-TILES.md in the Worker's repository), batched like the base layer's.

    Besides `tiles` and `last` (the contract), every batch says which list of places it was built for
    (`cells`, as the base layer does) and which run of each model it holds (`models`): the server can
    write both into its status without opening a tile, and ignores them if it does not need them.
    """
    return put_batches(target, "/api/admin/rain", tiles, {"cells": signature, "models": models}, max_tiles, max_bytes)


def put_batches(target: Target, path: str, tiles: dict[str, dict], extra: dict, max_tiles: int, max_bytes: int) -> int:
    """PUTs `{**extra, "last": ..., "tiles": {...}}` in batches; `last` is true on the final one only.

    With no tile at all one empty batch still goes out, marked last, so the server records the upload.
    Raises on any answer that is not 2xx (after retries). Returns the number of requests made.
    """
    parts = list(batches(tiles, max_tiles=max_tiles, max_bytes=max_bytes)) or [{}]
    for i, part in enumerate(parts):
        body = encode({**extra, "last": i == len(parts) - 1, "tiles": part})
        try:
            request(
                f"{target.url}{path}",
                method="PUT",
                data=body,
                user_agent=USER_AGENT,
                headers={"Authorization": f"Bearer {target.token}", "Content-Type": "application/json"},
            )
        except HttpError as err:
            raise RuntimeError(f"target {target.name}: upload {i + 1}/{len(parts)} refused with HTTP {err.status}: {err.body[:200]}") from None
        print(f"  {target.name}: batch {i + 1}/{len(parts)} stored ({len(part)} tiles, {len(body)} bytes)", flush=True)
    return len(parts)
