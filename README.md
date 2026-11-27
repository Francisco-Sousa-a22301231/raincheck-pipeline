# raincheck-pipeline

Builds the free, world-wide weather **base layer** of [Rain Check](https://raincheck.pt): a few numbers
per place (rain, evaporation, temperature, humidity, wind, cloud) for the past 72 hours and the next
72 hours, taken from the ECMWF IFS open-data forecast and packed into small JSON tiles.

A second job builds the **rain models** layer: the rain amount of NOAA GFS, DWD ICON-EU and ECMWF AIFS
and the rain chance and median of the NOAA GEFS ensemble, per place, for the next 72 hours (see
"Rain models" below).

It is an extractor and nothing else: it asks a Rain Check server which places it wants, downloads only
the model fields it needs, and uploads the tiles. It runs on GitHub Actions and keeps no state: every
run rebuilds everything from ECMWF.

## How it works

1. **Newest run.** IFS runs four times a day (00, 06, 12, 18 UTC) and is published 7 to 9 hours later.
   The script looks for the newest run whose 72-hour step is out (one or two small requests), and
   whose ENS (below) is out too: the ENS comes a few minutes after HRES, so until 10 hours after the
   run's time a run without it waits (the run before is taken, which the targets normally hold already);
   after that it is built without the rain chance.
2. **Who needs it.** For each target it reads `GET <url>/api/admin/base/status` (the run the server
   holds: one small stored object, no database behind it). A target that holds an older run, or none,
   gets the new one: only then is `GET <url>/api/cells` read (the places, with the same token; the server
   reads every one of them from its database for it). A target that already holds the run is left alone, unless the job
   was started with `--cells`: then its list of places is compared too, and a changed list rebuilds the
   same run. When no target needs anything the job ends there, in a few seconds and without installing
   anything.
3. **Download.** Seven surface fields per step: total precipitation `tp`, 2 m temperature `2t`, 2 m dew
   point `2d`, 10 m wind `10u` `10v`, total cloud cover `tcc`, surface solar radiation downwards
   `ssrd`; plus the land-sea mask `lsm` once. Each step file is about 100 MB; its `.index` file says
   where each field is, so only those bytes are fetched with HTTP range requests (about 3.3 MB per
   step).
   - the new run: steps 0 to 72 h, every 3 h (25 steps);
   - the 12 runs before it (72 h back): steps 3 and 6 h each, to rebuild the past 72 hours from the
     freshest forecast of every hour.

   That is 49 steps, about 165 MB and 400 requests per run, whatever the number of places.

   **Rain chance (ENS).** Then the ensemble of the same run (`ifs/0p25/enfo`, 50 perturbed members, same
   0.25° grid): each member's `tp` for steps 3 to 72 h, one range request each (about 0.45 MB; a step
   file is about 4.5 GB). About 550 MB and 1,224 requests more per run (measured 25/11/2026: 552 MB),
   whatever the number of places. When it cannot be read whole, the tiles go out without `p` (below) and
   the job does not fail.
4. **Sampling.** No interpolation: each place reads one grid point of the 0.25° grid, the nearest
   **land** point of the four around it (land = `lsm` ≥ 0.5), or the plain nearest point when none of
   the four is land. On a coast the plain nearest point is often sea, where the model has about twice
   the wind of the ground next to it; preferring land brought a seaside test place from 20 km/h to the
   11 km/h Open-Meteo gives for the same model.
5. **Numbers.** Accumulated fields (`tp`, `ssrd`) become amounts per 3-hour step; relative humidity
   comes from temperature and dew point; wind speed from `u` and `v`; `et0` from FAO-56
   Penman-Monteith (below). The rain chance `p` of a step is the share of the members with at least
   0.3 mm in it (0.1 mm an hour, the bar Open-Meteo uses for its `precipitation_probability`; at 12 wet
   places on 25/11/2026 this read 2.9 points under Open-Meteo's own ECMWF chance, 8.8 points apart on
   average), rounded to whole %; a step with fewer than 40 members read has none.
6. **Upload.** Tiles go to `PUT <url>/api/admin/base` in batches (at most 60 tiles and about 128 KB
   each: the server has little processor time per request), with retries. Any answer that is not 2xx fails the job. The last batch is marked, and only
   then does the server record the run as held: a run that broke half-way is done again next time.

## Tile format

One tile per 1° square, named `<floor(lat)>_<floor(lon)>` (Cascais, 38.70 N 9.38 W, is `38_-10`):

```json
{ "v": 1, "model": "ifs025", "run": 1795435200, "made": 1795470640,
  "t": [1795186800, 1795197600],
  "zones": { "<place id>": { "rain": [0.2, 0], "et0": [0.05, 0.1], "temp": [14.1, 15.0],
                             "rh": [81, 77], "wind": [12.3, 9.8], "cloud": [90, 40], "p": [null, 64] } } }
```

- `run`: start of the model run; `made`: when the tile was built (both unix seconds).
- `t`: the **end** of each 3-hour step, unix seconds, ascending: 48 values, from 69 h before the run to
  72 h after it.
- `rain`, `et0`: mm inside the step. `temp` °C, `rh` %, `wind` km/h (10 m), `cloud` %: the state at the
  end of the step. Every array is as long as `t`; a missing value is `null` (for example the oldest
  hours when an earlier run is no longer served).
- `p` (optional): rain chance in the step, %, from the ENS; only for the steps after the run (`null`
  before, and where it is not known). Absent when the run was built without the ENS.
- Past steps come from the run that started 3 or 6 hours before them. At the run time itself the state
  is the new run's analysis (step 0).

Upload body: `{ "run": <run>, "cells": "<fingerprint of the list of places>", "last": true|false,
"tiles": { "<key>": <tile> } }`.

## Reference evaporation (`et0`): what is simplified

FAO Irrigation and Drainage Paper 56 (Allen, Pereira, Raes, Smith, 1998), the hourly equation (eq. 53),
applied to a 3-hour step with the step's mean values and multiplied by 3:

- **Mean state** of a step = the average of its start and its end (temperature, vapour pressure from
  the dew point, wind, cloud). The model only gives the state every 3 hours.
- **Wind** at 2 m = 0.748 × wind at 10 m (eq. 47).
- **Altitude is taken as 0 m** for every place (air pressure 101.3 kPa, clear-sky factor 0.75). At
  1000 m this changes `et0` by a few per cent.
- **Net radiation** is estimated the FAO-56 way from the solar radiation reaching the ground (`ssrd`):
  albedo 0.23; clear-sky radiation Rso = 0.75 × extraterrestrial radiation, integrated over the step
  between sunrise and sunset (eq. 28); net long-wave radiation from eq. 39.
- **At night and with a very low sun** (Rso under 0.3 MJ m⁻² h⁻¹) the ratio Rs/Rso means nothing, so the
  cloudiness term of eq. 39 uses the model's cloud cover instead: Rs/Rso = 1 − 0.75 c^3.4 (Kasten and
  Czeplak, 1980). FAO-56 suggests reusing the ratio of the hours before sunset.
- **Soil heat flux** = 0.1 × Rn when Rn is positive, 0.5 × Rn when it is negative (eq. 45 and 46).
- Negative results (dew) are 0.

The tests check the equation against FAO-56 example 19 (N'Diaye, Senegal, 1 October, 14:00 to 15:00:
0.63 mm/hour; 02:00 to 03:00: 0.00 mm/hour).

## Run it

Python 3.11 or newer.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest

# No server involved: the places come from a file, the tiles go to a folder.
python -m raincheck_pipeline --dry-run --points-file points.example.json --out out

# The same tiles against Open-Meteo's copy of the model (one request per place).
python scripts/compare_open_meteo.py points.example.json out

# Against a server (here a local one).
TARGETS=LOCAL LOCAL_URL=http://127.0.0.1:8787 LOCAL_TOKEN=... python -m raincheck_pipeline
```

GRIB decoding uses [ecCodes](https://confluence.ecmwf.int/display/ECC) through the `eccodes` package,
whose Linux wheel brings its own binary (IFS files are CCSDS-compressed, which needs it). On Windows
the same wheel may be refused by Smart App Control; a Linux container works:

```bash
docker run --rm -v "$PWD:/app" -w /app python:3.12-slim sh -c \
  "pip install -q -r requirements.txt && python -m raincheck_pipeline --dry-run --points-file points.example.json"
```

Options: `--check` (only say whether there is work, no download), `--cells` (also compare the list of
places of a target that already holds the run), `--force` (upload even when the target holds the run),
`--run YYYYMMDDHH` (a given run instead of the newest), `--no-ens` (leave the rain chance out: no wait
for the ENS, no download). `python scripts/measure_ens.py POINTS.json [--om N]` measures the ENS part
alone (bytes, requests, seconds) and compares its chance with Open-Meteo's at N places.

## Settings (environment)

| Name | What |
|---|---|
| `TARGETS` | Comma-separated names of the servers to feed, for example `STAGING,LIVE`. |
| `<NAME>_URL` | Address of the server, for example `https://staging.example.org`. Must be https (plain http only for localhost). |
| `<NAME>_TOKEN` | The server's pipeline token (sent as `Authorization: Bearer` with all three requests: status, places, upload). A target without it is skipped with a message. |
| `ECMWF_SOURCES` | Where to download from, in order. Default `ecmwf,aws,gcs`: ECMWF's portal, then its mirrors at AWS and Google Cloud (which also keep older runs). Names from that list, or base URLs. |
| `DOWNLOAD_THREADS` | Parallel downloads, default 8. |
| `BATCH_TILES`, `BATCH_BYTES` | Upload batch limits, default 60 tiles and 131072 bytes (the server takes at most 300 tiles and 1 MB). |

On GitHub Actions (`.github/workflows/base-layer.yml`) the names are repository **variables**
(`TARGETS`, `STAGING_URL`, `LIVE_URL`) and repository **secrets** (`STAGING_TOKEN`, `LIVE_TOKEN`). The
workflow runs every 15 minutes and on demand ("Run workflow"), never two at once. Three checks an hour
only ask for a new run; the fourth (and a manual run) passes `--cells`.

GitHub turns scheduled workflows off in a public repository after 60 days without any activity in it
(a commit is enough to keep them on, and GitHub sends an e-mail before). Nothing here works around
that.

## Limits

(Of the base layer; the rain models' are in "Rain models" below.)

- **Resolution:** 0.25° (about 28 km) and 3-hour steps: showers and sea breezes are smoothed, places in
  the same grid box get the same numbers, and mountains and valleys are not told apart (no correction
  for altitude).
- **Delay:** a run is out 7 to 9 hours after its time, the job notices within 15 minutes or so (GitHub
  starts scheduled jobs late when it is busy), so tiles are 7 to 15 hours behind the atmosphere.
- **The past is forecast, not measurement:** the 3 to 6 hour forecasts of earlier runs.
- **Earlier runs:** the ECMWF portal keeps the last four dates only, so the oldest run an 18 UTC run
  needs (72 h back) always comes from a mirror (AWS, then Google Cloud; both keep every run, and the AWS
  one often answers "SlowDown"). When every source says an earlier run is not there (404) its hours are
  `null`. When a source cannot be asked, a field cannot be downloaded, or a step or field of the new
  run is missing, the job fails and nothing is uploaded: the next job tries again.
- **New places** appear within the hour or so after the server lists them (their list has a
  fingerprint, compared once an hour: a change rebuilds the tiles even for the same run), and at the
  next run in any case.

## Rain models (`rain/v1`)

`python -m raincheck_pipeline rain` (`raincheck_pipeline/rain.py`, workflow `rain-models.yml`) adds four
more models to the base layer's ECMWF IFS and ENS, so every place gets several opinions on rain without
any Open-Meteo call. The contract with the Worker is `docs/RAIN-TILES.md` in its repository.

| Model | What | Source | Per run (measured 27/11/2026) | Out after the run |
|---|---|---|---|---|
| `gfs` | NOAA GFS 0.25°, deterministic | `noaa-gfs-bdp-pds` on AWS: the APCP record of each step, by range request after its `.idx` | about 15 MB, 52 requests | about 4.1 h |
| `gefs` | NOAA GEFS 0.25°, 31 members | `noaa-gefs-pds` on AWS (`pgrb2sp25`), the same way per member | about 220 MB, 1,550 requests | about 4.4 h |
| `icon_eu` | DWD ICON-EU 0.0625°, Europe only | `opendata.dwd.de`: whole bzip2 files of `TOT_PREC`, one per step | about 17 MB, 26 requests | about 3.3 h |
| `aifs` | ECMWF AIFS single 0.25° (machine-learned), 6-hourly | ECMWF open data (portal, AWS, Google Cloud), `tp` by range request | about 6 MB, 30 requests | about 6.1 h |

How it works:

1. **Timeline.** 24 three-hour steps: the end of the step the job starts in (`made`), then every 3 h.
   Each model fills it from its own newest run, so the same step is step 15 of a GFS 00 UTC run and
   step 9 of an ICON-EU 06 UTC run. Only the forecast steps the timeline needs are fetched (a run a few
   hours old needs steps up to about 80 h, not just 72).
2. **Newest run, model by model.** The newest 00/06/12/18 UTC run whose every needed step is published
   (HEAD requests, the last step first: NOAA does not publish steps strictly in order; on 27/11/2026 at
   run + 4 h GFS had f075 but not f060), else the run before (up to 18 h back). A model whose server
   cannot be asked is left out of the job.
3. **Who needs it.** Each target's `GET /api/admin/rain/status` says which run of each model it holds
   (`models`). A target gets a build when any model has a newer run (each model comes out at its own
   time, so a 6-hour cycle can give up to four builds), or, with `--cells`, when its list of places
   changed. Only then is `GET /api/cells` read.
4. **Download and sample**, model by model, `DOWNLOAD_THREADS` at a time, each field read at the places
   and dropped at once (at most two downloads per thread wait to be decoded). Each place reads one grid
   point per model: the nearest land point as in the base layer (GFS's `LAND` field of step 0, also used
   for GEFS; ICON-EU's own land fraction `FR_LAND`; AIFS's `lsm`). GFS's longitudes run 0 to 360 E,
   ECMWF's from 180 W, ICON-EU's from 336.5 E: the grid code works on any of them.
5. **Amounts.** Accumulations become amounts per 3-hour step: GFS and ICON-EU totals since the run's
   start (`total(L) - total(L-3)`); GEFS buckets that start again every 6 hours (at 3, 9, 15... h the
   bucket is the step; at 6, 12, 18... h the bucket of the step before comes off); AIFS totals every
   6 hours, each 6-hour amount split evenly over its two 3-hour steps. GRIB packing leaves small negative
   differences (down to -0.09 mm in GEFS): clipped at 0. `gefs_p` is the share of the members with at
   least 0.3 mm in the step (the ENS rule), whole %; `gefs_mm` their median; both null where fewer than
   25 members are known.
6. **Failures.** A deterministic model with a step missing or a download that fails is left out of the
   tiles (logged); a GEFS member that fails is left out, GEFS itself when fewer than 25 members remain.
   The other models still go out. When no model could be read nothing is uploaded.
7. **Upload** to `PUT /api/admin/rain` in batches (at most 60 tiles and about 128 KB), `last: true` on the
   final one. Every batch also carries `cells` (the list fingerprint, as for the base layer) and `models`
   (`{ <model>: <run> }`).

Tile (`rain/v1/<floor(lat)>_<floor(lon)>.json`):

```json
{ "v": 1, "made": 1795773398, "t": [1795780800, 1795791600],
  "models": { "gfs": { "run": 1795737600 }, "gefs": { "run": 1795737600, "members": 31 },
              "icon_eu": { "run": 1795759200 }, "aifs": { "run": 1795737600 } },
  "zones": { "<place id>": { "gfs": [0.2, 0], "icon_eu": [0.1, 0], "aifs": [0.2, 0.1],
                             "gefs_p": [10, 26], "gefs_mm": [0, 0.1] } } }
```

Amounts in mm in the step (0.1 mm), `null` where the model's run doesn't cover the step; `icon_eu` is
absent for places outside its domain (29.5 to 70.5 N, 23.5 W to 62.5 E); a model left out is absent from
`models` and from every zone.

```bash
python -m raincheck_pipeline rain --dry-run --points-file points.example.json --out out-rain
python scripts/compare_rain_open_meteo.py points.example.json out-rain 4   # against Open-Meteo's copies
```

Limits: one grid point per model, no interpolation (ICON-EU's is about 7 km, the others about 28 km);
AIFS says nothing about how rain falls inside its 6 hours (halved); the job keeps no state, so every
build downloads every model again (about 260 MB, GEFS most of it), up to four times a cycle as the
models come out; a model's run is held until its next one is complete, so a model that is late keeps
its previous run (and a tile older than 36 h is ignored by the Worker).

Options: `--check`, `--cells`, `--force` as for the base layer, `--models gfs,icon_eu` (only those). The
workflow runs at :26 (with `--cells`) and :56 every hour, uses the same variables and secrets as the base
layer, and never runs twice at once.

## Data source and licence

Weather data: **ECMWF open data**, IFS high-resolution forecast and ensemble (ENS) at 0.25°
(<https://www.ecmwf.int/en/forecasts/datasets/open-data>), read from <https://data.ecmwf.int/forecasts/>
and from its mirrors on AWS (`s3://ecmwf-forecasts`) and Google Cloud (`gs://ecmwf-open-data`). It is
published under the Creative Commons Attribution 4.0 International licence (CC BY 4.0,
<https://creativecommons.org/licenses/by/4.0/>). The tiles are derived from it (sampled at places,
turned into other units and quantities). The portal allows a limited number of connections at once; this
job uses 8.

> Copyright © 2026 European Centre for Medium-Range Weather Forecasts (ECMWF). Source: www.ecmwf.int.
> This data is published under a Creative Commons Attribution 4.0 International (CC BY 4.0).
> https://creativecommons.org/licenses/by/4.0/
> ECMWF does not accept any liability whatsoever for any error or omission in the data, their
> availability, or for any loss or damage arising from their use.

The data was modified: see "How it works". A site that shows numbers from these tiles has to carry the
same attribution.

The rain-models job (above) also reads:

- **ECMWF AIFS single** open data (<https://data.ecmwf.int/forecasts/>, `aifs-single`): the same ECMWF
  terms and CC BY 4.0 attribution as above.
- **NOAA GFS and GEFS**, from the NOAA Open Data Dissemination program on AWS
  (`s3://noaa-gfs-bdp-pds`, `s3://noaa-gefs-pds`). Data of the US National Weather Service is in the
  public domain; credited as a courtesy: "Source: NOAA / National Centers for Environmental Prediction
  (NCEP)". NOAA does not endorse this product.
- **DWD ICON-EU**, from the open-data server of Deutscher Wetterdienst (<https://opendata.dwd.de/>),
  under CC BY 4.0. Attribution: "Source: Deutscher Wetterdienst" (the data was modified: sampled at
  places, accumulations turned into 3-hour amounts).
