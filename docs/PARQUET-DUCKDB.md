# UK lightning: Parquet store and DuckDB SQL layer

**Status: draft data plane. Not merged and not deployed. Nothing here touches GridAtlas.** This
branch (`data/parquet-duckdb-20261026`) builds on PR #2 (`data-plane/hourly-lookback-index-v2`)
and inherits its coverage semantics unchanged. It is laid out like the estate's data
repositories. `data-grid-gb` fetches, pins by SHA-256, derives and verifies fail-closed before
publishing. `data-gb-electricity` holds partitioned ZSTD Parquet and a small browser rollup
that is re-checked against the Parquet with DuckDB.

## What the data is, and is not

- Every row is an **optical lightning flash seen from orbit by the EUMETSAT MTG Lightning
  Imager (L2-LFL)**. That includes flashes inside clouds. They are **not ground strikes**, not
  confirmed cloud-to-ground strikes, **not damage** to anything, and **not evidence of
  causality**: they do not show that an electrical fault, outage or trip was caused by
  lightning. Location accuracy is several kilometres (about 4.5 km at nadir, coarser at UK
  latitudes).
- **The archive is prospective.** It starts when the hourly job first runs with PR #2's code.
  There is no back-data. Nothing is backfilled, interpolated, estimated or synthesised.
- **The seed is only partly searched.** The 408 flashes of 21–25 Oct 2026 came from the
  earlier 7-day feed, whose collector searched only part of each day: 21 Oct 100 %, 22 Oct
  94 %, 23 Oct 49 %, 24 Oct 58 %, 25 Oct 59 % (`data/seed_scan_windows.json`). Flashes in
  unsearched hours are **unknown, not absent**. The rollups therefore say
  `count_state: lower_bound_partial_coverage`.
- **Never searched is NULL, never zero.** A lost shard reads **UNKNOWN**, never zero. A count
  of 0 appears only for time that was actually searched.
- The archive covers one calendar year, but for a long time most of its days will say "never
  searched". No consumer may describe it as "a year of lightning".

## Layout

```
data/parquet/
  flashes/year=YYYY/month=MM/part-0.parquet       one row per archived flash
  coverage/days.parquet                           one row per UTC date of the index
  coverage/scan_windows.parquet                   merged successful search windows
  coverage/slots/year=YYYY/month=MM/part-0.parquet one row per 10-minute LI slot
  rollups/daily.parquet
  rollups/hourly/year=YYYY/month=MM/part-0.parquet
  rollups/substation_optical_flash_proximity.parquet
  rollups/substation_optical_flash_proximity_daily.parquet
  rollups/storm_episodes.parquet
  rollups/headline.json                           <= 4096 bytes, for browsers
  manifest.json                                   every file: bytes, sha256, rows, row groups,
                                                  min/max time, schema version
sql/*.sql                                         DuckDB views (run in file order)
sources/gb-transmission-substations.source.json   SHA-256 pin of data-grid-gb connection points
sources/gb-transmission-substations.v1.json       located-substation extract (489 of 886)
```

Partitions are UTC year and zero-padded month. (`data-gb-electricity` uses unpadded
`month=6`; this store uses `month=06` as the work order specifies. DuckDB's hive partitioning
reads both.)

### Flashes (`uk-lightning-parquet-flashes.v1`)

| column | type | meaning |
|---|---|---|
| `id` | string | the archive feature id `<EUMETSAT product name>:<index>` (stable, unique) |
| `time_utc` | timestamp[us, UTC] | flash time, exactly as archived |
| `lon`, `lat` | float64 | as archived (6 dp), always inside the UK box −11..3 E, 49.5..61.5 N |
| `source_product` | string | `MTG LI L2-LFL` |
| `observation_kind` | string | `optical_lightning_flash` |
| `ingest_run_id` | string | the workflow run (`gh-<run_id>-<attempt>`) that first wrote this flash into the Parquet store. It is carried forward unchanged after that. |

The rows are exactly the records `history.py` wrote into the JSON day shards, and nothing else.
`parquet_store.read_archive` first checks every shard's bytes and SHA-256 against index v2. It
then re-validates every record with `history.validate_feature`, rejects duplicates, and
requires the per-day counts to equal the index. Shards on disk must equal the shards listed.
A non-synthetic build also refuses any id that is not a genuine EUMETSAT LI product name.

Writer settings:

- ZSTD level 9.
- Rows sorted by `(time_utc, id)`.
- Row groups of up to 32,768 rows. In the synthetic storm-year test these are a few hundred kB
  each, so one HTTP range read fetches one group. Column statistics and the page index allow a
  date-range query to skip every other group.
- Time and integer columns are delta-encoded. Repeated strings are dictionary-encoded.

The output is deterministic: the same archive and the same library versions give the same
bytes, so a run that changes nothing commits nothing.

### Coverage (`coverage/`)

- `days.parquet` copies index v2 row for row. Its columns are:
  - `coverage`: `none | partial | full | shard_lost`
  - `count_state`: `observed | UNKNOWN`
  - `hours_observed` / `hours_expected`
  - `scanned_seconds`, `flash_count` and `slots_searched`, which are **NULL iff the coverage
    is `none` or `shard_lost`**
  - `sealed`, and the first and last observation.
- `slots/` has one row for each 10-minute LI slot (phase HH:M0:09) from the cutoff to today.
  - `scanned_seconds` is the slot's overlap with the search windows.
  - `basis` is `scan_window`, or `flash_proven`. A slot is flash-proven when an archived flash
    shows that its product was examined but no window was recorded (seed data). Flash-proven
    seconds are a lower bound.
  - `flash_count` counts each flash in its product's slot. It is NULL for never-searched slots.
- A row marked `shard_lost: true` in index v2 becomes `coverage: shard_lost` and reads UNKNOWN
  in every table. PR #2's `history.py` never writes this flag; it is accepted
  forward-compatibly. A shard that is missing **without** the flag fails the build: it is
  never silently read as zero.

## DuckDB SQL layer (`sql/`, `rollups.py`)

`rollups.connect(store)` creates the views below over the Parquet. You can also query from the
command line, or write a small file holding only views (gitignored, never committed):

```
python rollups.py query "SELECT * FROM daily_optical_flashes WHERE count_state = 'observed'"
python rollups.py duckdb --out lightning.duckdb
```

| view / output | what it is |
|---|---|
| `daily_optical_flashes` → `rollups/daily.parquet` | daily counts joined to `coverage_days`. `coalesce(…, 0)` is applied **only** when `count_state = 'observed'`. |
| `hourly_optical_flashes` → `rollups/hourly/…` | 24 rows per date. `scanned_seconds` is the overlap with the search windows; a seed hour without a window is `flash_proven` (600 s per occupied slot, a lower bound). Unsearched and lost hours are NULL. |
| `substation_optical_flash_proximity` | **optical-flash proximity** (described below), with `hours_observed`, `hours_expected`, `days_full/partial/never_searched/shard_lost` and a `count_state` |
| `substation_optical_flash_proximity_daily` | the same per site and UTC day, only for rows with at least one flash within 20 km. A missing row means 0 on a searched day and UNKNOWN otherwise, so join `coverage_days`. |
| `storm_episodes` | time clusters of flashes anywhere in the box (described below) |
| `rollups/headline.json` | the browser rollup: totals, day coverage, the last 7 days, the busiest day, the top 3 sites by 10 km proximity, and episodes. Every count travels with its coverage. |

### Optical-flash proximity (not strikes, not faults)

`optical_flash_proximity_5km`, `_10km` and `_20km` count the satellite optical flashes whose
reported location is within that great-circle distance (haversine, R = 6371.0088 km) of a
transmission connection point's **joined map location**.

- These are **not ground strikes**, **not confirmed strikes** and **not electrical faults**,
  and they say nothing about damage.
- The 5 km ring is at or below the sensor's location accuracy.
- Counts cover searched hours only. With partial coverage they are lower bounds
  (`count_state: lower_bound_partial_coverage`).
- Each count column carries this label in its Parquet field description and in the DuckDB view
  comment, and `verify_parquet.py` fails if the label is removed.

The substations come from data-grid-gb's `derived/connection-points.v3.json` at commit
`5181de3`, pinned by SHA-256 (`8db7171d…`, 2,934,509 bytes):

- 489 of the 886 connection points carry a joined location. The 397 without one are counted
  and left out; no coordinates are guessed.
- `tools/extract_substations.py --fetch --check` re-downloads the pinned raw URL, refuses any
  other bytes, and confirms that the committed extract is derived from those bytes.
- The test workflow runs this check.

### Storm episodes

Consecutive flashes, ordered by time, belong to the same episode only when they are at most
30 min apart **and** the whole gap between them lies inside one searched window.

- A gap that crosses unsearched time always starts a new episode, with
  `start_reason = unsearched_gap`, because we cannot know whether that time was quiet.
- `searched_30min_before` and `searched_30min_after` record whether the episode's edges were
  searched. An episode counts as `complete` only when both were.
- An episode is a time cluster across the whole UK box. It is **not a single storm cell**:
  simultaneous storms merge into one episode. It also says nothing about ground strikes.

## Manifest and fail-closed verification

`manifest.json` (`uk-lightning-parquet-manifest.v1`) records:

- every file's path, bytes, SHA-256, rows, row groups, schema version, time column and its
  min/max;
- the archive totals the store was derived from;
- the substation pin;
- the writer settings.

It holds no wall-clock timestamp, so an unchanged store gives an unchanged manifest.

`verify_parquet.py` re-reads the JSON archive and the Parquet, and recomputes every number in
plain Python rather than with the SQL that built it. It exits non-zero if any of these is true:

- a file is missing or unlisted, or its bytes, SHA-256, rows, row groups, schema or min/max
  time disagree with the manifest;
- a flash lies outside the UK box, is not finite, is duplicated, sits in the wrong partition,
  has a non-genuine id, falls outside its product's sensing slot (±60 s; a real flash was seen
  0.361 s before its product's nominal start), or differs from its archive record;
- a count, `hours_observed` or `scanned_seconds` is claimed for never-searched or lost time;
  this includes a **zero for an unsearched day**;
- any daily, hourly, slot, proximity, episode or headline number disagrees with the
  recomputation;
- `headline.json` exceeds 4096 bytes;
- the store is marked synthetic.

## Workflow

`update-lightning.yml` keeps PR #2's cron (`17 * * * *`) and its order: feed first, then the
feed commit, the archive, the status and the archive commit. After those it adds:

1. **Build Parquet, coverage, rollups and manifest.** This runs only if the archive step
   succeeded, uses `--if-archive`, and has `continue-on-error`.
2. **Verify the Parquet store (fail closed).**
3. **Commit the Parquet store.** It stages `data/parquet` only, commits only if something
   changed, and makes a separate commit.
4. The final step also turns the job red when the Parquet build or its verification failed.

A Parquet failure never blocks the feed or the archive commit. `test-lightning.yml` installs
`requirements.txt` (`duckdb==2.0.0` and `pyarrow==26.0.0`, pinned) before it runs the tests.

## Sizes

These figures come from the real seed: the archive that the first run would write from the
408 committed flashes and the seed windows.

| what | bytes | per flash |
|---|---|---|
| JSON day shards (`data/history/days/…`) | 216,561 | 530.8 |
| Parquet flashes | 12,356 | 30.3 |
| whole Parquet store (34 files, incl. a 366-day coverage grid, rollups and manifest) | 162,082 | — |
| `headline.json` | 1,970 | — |

**SYNTHETIC storm year. These figures come from invented data, generated only in a temp dir
and never published** (`python tools/stress_storm_year.py --flashes 1000000`; Python 3.13,
duckdb 2.0.0, pyarrow 26.0.0, 2 vCPU sandbox).

The test generated 1,000,000 flashes over 89 storm days. The busiest day had 47,443 flashes
and was split into hourly JSON parts. Every day was fully searched.

| what | value |
|---|---|
| JSON day shards | 537,167,717 B (537.2 B/flash) |
| Parquet flashes | 17,992,654 B (18.0 B/flash): **29.9× smaller** |
| whole Parquet store (all tables) | 18,208,788 B |
| flash row groups | 36 groups of 108–586 kB (mean 489 kB); a one-day query touches 2 of them |
| build time: JSON archive / Parquet + rollups / fail-closed verify | 48 s / 75 s / 42 s (peak RSS 1.8 GB) |
| DuckDB query times: count / daily / hourly / one day of points | 0.02 s / 0.09 s / 0.62 s / 0.007 s |
| DuckDB query times: optical-flash proximity for 489 sites / storm episodes | 9.6 s / 1.8 s |

On a storm year, the whole box merges into one episode per busy day. This is expected: an
episode is a time cluster, not a storm cell.

## Licensing and attribution

- **EUMETSAT**: the MTG LI L2-LFL collection `EO:EUM:DAT:0691` is listed with the
  `NoConditions` data policy. Attribution to EUMETSAT is retained in the manifest, the Parquet
  metadata and the headline.
- **OpenStreetMap**: substation coordinates are © OpenStreetMap contributors and are available
  under the Open Database Licence (ODbL 1.0). data-grid-gb joined them to the ETYS identities
  via the GridAtlas release. The extract and every rollup that republishes those coordinates
  carry the ODbL attribution, and ODbL share-alike applies to them.
- **NESO**: site names, owners and voltages come from NESO's *Electricity Ten Year Statement
  2025*, Appendix B, as derived by data-grid-gb. data-grid-gb has no LICENSE file at the
  pinned commit, so **the owner should confirm the terms before wider redistribution.**
- The code in this repository is under the Apache License 2.0, the same licence as GridAtlas,
  with a NOTICE in the same form (LICENSE is byte-identical to Ventusltd/gridatlas). NOTICE lists the
  third-party data that keeps its own terms.

## Limitations

- The archive is prospective, and its seed was partly searched. Most of the year reads UNKNOWN.
- LI detection efficiency is about 70–95 % and its location accuracy is several kilometres.
  A searched hour with 0 flashes means none were *detected and archived*, not that there was
  no lightning.
- The 9 s slot phase and flash-proven seconds are approximations of what was examined. Both
  are documented and conservative.
- With an hourly cron, the coverage tables change every successful run. Expect one small
  Parquet commit per hour: roughly the current month's slot and hourly files plus the day
  table and rollups, a few tens of kB. Binary Parquet does not delta-compress in git. If repo
  growth matters, the owner may prefer a daily Parquet commit; that is a one-line `if:` change.
- The headline's `last_7_days` uses `MM-DD` keys to stay under 4 kB.
- A browser DuckDB-wasm Parquet path was not tested here. The Claude sandbox blocks
  DuckDB-wasm's extension download, so it is **BLOCKED**, not PASS.
