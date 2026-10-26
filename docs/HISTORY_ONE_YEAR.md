# UK lightning: hourly collection, one-year UTC archive (index v2) and the date-window client

**Status: data plane only. Nothing in this repository touches GridAtlas's shell, UI, map,
menus or fetch bridge. Draft for review; not merged, not deployed.** This document replaces
the PR #1 draft (`feature/one-year-utc-lightning-history`), whose history.py,
history-window.mjs, tests and this file were the starting point.

## What the data is, and is not

- Flashes are **optical lightning flashes seen by the MTG Lightning Imager (EUMETSAT L2-LFL)**,
  including intracloud lightning. They are **not confirmed cloud-to-ground strikes** and never
  evidence that a particular place was struck.
- The archive is **prospective**. It starts the first time the hourly job runs with this code.
  There is **no back-data**: EUMETSAT history before that has not been downloaded, and nothing
  is interpolated, estimated or back-dated.
- The only earlier content is the **seed**: the flashes already in the 7-day live feed when the
  archive is first written. On 26 Oct 2026 that is 408 flashes from 21 Oct 12:32 to 25 Oct 11:15 UTC
  (31 / 67 / 189 / 118 / 3 per UTC day). The collector that produced them ran every three hours
  with no lookback and GitHub started only 4-5 of the 8 scheduled runs per day, so the seeded
  days were **only partly searched**: 21 Oct 100 %, 22 Oct 94 %, 23 Oct 49 %, 24 Oct 58 %,
  25 Oct 59 % of the day (reconstructed from the committed `query_start/end_utc` of every feed
  version; `data/seed_scan_windows.json`). Flashes during the unsearched hours are **unknown,
  not absent**.
- A **missing day is UNKNOWN, never zero**. The index says so per day (`coverage: "none"`,
  `count: null`). A `count` of `0` is only ever written for time that was actually searched.
- The index covers one calendar year of UTC dates, but for a long time most rows will say
  "never searched". No consumer may call the first year "a year of history".

## Collector (collect.py) and workflow

`.github/workflows/update-lightning.yml` runs **hourly** (`17 * * * *`). Each run:

1. **Lookback window** = `max(6 h, now − last successful scan end + 30 min)`, **capped at 48 h**
   (`collect.lookback_window`). The last successful scan end comes from `data/status.json`, else
   from the archive index, else the 6 h default. A late or failed run is therefore caught up by
   the next successful one; a gap older than 48 h can no longer be searched by the schedule and
   stays unknown (`lookback_capped: true` in the run record and status).
2. **Examined 10-minute slots.** Every LI product covers a 10-minute sensing slot encoded in its
   name (`…_OPE_<start>_<end>_…`). Only the union of examined slots counts as *searched*
   (`collect.scanned_ranges`). The slot containing `now` is normally not yet published, so the
   tail of every query window is unobserved and recorded as `tail_unobserved_seconds`; a missing
   slot in the middle is a source gap (`unscanned_ranges`). Zero products returned is a failure,
   not an empty sky.
3. Writes the **feed first** (`data/uk_lightning.geojson`, 7-day retention, 40 000 display cap,
   byte-compatible with the previous feed plus additive metadata: `slots_examined`,
   `scan_end_utc`, `published_at_utc`, `first/last_detection_utc`, `coverage_note`).
4. Leaves a **run record** in `$RUNNER_TEMP` (never committed) with the uncapped incoming flashes,
   the previously published feed features (the seed), the examined slots and the searched ranges.

The workflow then commits the **feed on its own**, runs `history.py ingest` as a separate step
(`continue-on-error`), writes `data/status.json` in an **always-run** step (`status.py`), commits
**archive + status separately**, and finally fails the job if collection or archive failed so
the Actions UI shows red. **An archive failure never blocks the feed.**

`data/status.json` (`uk-lightning-status.v1`): `health` (ok / warn > 6 h / stale > 12 h /
stopped > 24 h / never), `last_attempt_utc`, `last_attempt_outcome`, `last_error`,
`consecutive_failures`, `last_successful_scan_start/end_utc`, the last run's lookback, slots,
searched and unsearched ranges, feed counts and the archive outcome.

## Archive layout

```
data/history/index.json                 uk-lightning-history-index.v2
data/history/days/YYYY/MM/DD.json       uk-lightning-history-day.v1 (one UTC day)
data/history/days/YYYY/MM/DD/HH.json    hourly parts of a day above the 8 MiB target
data/status.json                        collector heartbeat (see above)
data/seed_scan_windows.json             one-off seed of reconstructed search windows
```

Day files are GeoJSON FeatureCollections saved as **`.json`**: the original GridAtlas fetch
bridge intercepts every `.geojson` URL, cross-origin included, but passes `.json` through.

### Index v2 (`uk-lightning-history-index.v2`)

Top level (the F8 contract of the V6 pattern study, with additive fields marked +):

| field | meaning |
|---|---|
| `schema` | `uk-lightning-history-index.v2` |
| `generated_utc` | when this index was written (use it for "refreshed", not fetch time) |
| `collection_interval_hours` | 1 |
| `collection_lookback_hours` | 6 (minimum self-healing lookback); + `collection_lookback_hours_max` 48, + `last_run_lookback_hours` |
| `next_update_due_utc` | `generated_utc` + interval + 2 h grace for cron drift; consumers treat `now > next_update_due_utc` (or age > 4 h) as **STALE** |
| `cutoff_date_utc` | first retained UTC date (one calendar year back, leap-day safe) |
| `retention`, `source`, `source_url`, `observation`, `bounds_wsen` | provenance; `observation` is literally `satellite optical lightning flashes, not ground strikes` |
| `max_shard_bytes` | 25 000 000: a shard that would exceed it **fails the run loudly**; + `target_shard_bytes` 8 388 608 |
| `archived_observations`, `first_observation_utc`, `last_observation_utc` | totals over the archive |
| `scan_windows[]` | merged successful search ranges `{start_utc, end_utc, successful_runs}` (union of examined slots) |
| `days[]` | **one row per UTC date from `cutoff_date_utc` to today** (≤ 366 rows, ~70 KB raw) |
| `status` | `archive_partial_until_history_is_genuinely_collected_or_backfilled` |
| + `archive_first_enabled_utc` | when the archive files began |
| + `coverage_start_utc` | earliest searched instant (use this, not `archive_first_enabled_utc`, for "before the archive") |
| + `last_successful_scan_end_utc` | what the next collector run looks back to |
| + `archived_days_with_flashes`, `scanned_days`, notes | counts and the honesty notes |

Day row, in this key order:

```json
{"date":"2026-10-23","coverage":"partial","scanned_seconds":42472,"count":189,"bytes":99892,
 "path":"days/2026/10/23.json","sealed":true,"first_observation_utc":"2026-10-23T01:53:48.223Z",
 "last_observation_utc":"2026-10-23T19:50:08.639Z","sha256":"…64 hex…"}
```

Rules:
- `coverage` ∈ `none | partial | full`; `scanned_seconds` (0–86400) is the searched time of the
  date from `scan_windows`. A day with archived flashes but no recorded window gets `partial`
  with the 10-minute slots its flashes prove were examined (a lower bound).
- `count` is **`null` iff `coverage == "none"`**; `0` only with `scanned_seconds > 0`.
- `path` is `null` when `count` is `null` or `0`, **or** when the day is split into hourly parts:
  then `count > 0`, `path` is `null`, and `parts[]` lists
  `{hour, path: "days/YYYY/MM/DD/HH.json", count, bytes, sha256, first/last_observation_utc}`;
  `bytes` is the sum of the parts and `sha256` is `null`. Fetch only the parts overlapping the
  wanted hours.
- `bytes` and `sha256` describe the shard file exactly (a reader can detect a mid-write skew
  between index and shard; raw.githubusercontent.com caches each URL for up to 5 minutes).
- `sealed` = every slot of the date was examined (`full`) **or** `generated_utc ≥ date + 1 d +
  48 h`, i.e. no scheduled run can touch the shard any more: safe for ordinary HTTP caching
  (`cache: 'default'`). Unsealed shards and the index use `cache: 'no-cache'` (revalidate).
- `shard_lost: true` (optional key) marks a date whose listed shard or part vanished from disk:
  the row becomes `coverage none`, `scanned_seconds 0`, `count null` (unknown, never zero),
  stays so on later runs (incoming flashes for it are dropped and counted as `dropped_lost_day`),
  and any remaining files are left unlisted for manual recovery from git history.

### Day shard (`uk-lightning-history-day.v1`)

Unchanged feature shape from `collect.build_feature` (526 bytes per flash: product-name id,
`time_utc`, `source`, `source_product`, `observation_kind`, `name`, `raw_tech`, `status`).
`metadata`: `date_utc`, `source`, `observation`, `count`, `first/last_observation_utc`; hourly
parts add `hour_utc` and `part_of_day: true`. Shards are written byte-stably (atomic replace),
deduplicated by the stable feature id (existing bytes win), sorted by `time_utc` then id.
`sealed` lives in the index only, so sealing never rewrites a shard.

### Sizes, splitting, growth

A day at or below **8 MiB** is one file (≈ 16 000 flashes at the current shape). Above it the day
is split into hourly parts. Any single file above **25 000 000 bytes** raises `ShardTooLarge` and
the archive step fails (feed unaffected; status records the error). So no file can approach
GitHub's 50 MiB warning, let alone the 100 MiB block, even on a 10⁵-flash storm day.
Untouched days are verified by `bytes` + `sha256` on every run instead of re-parsed, so the run
cost is O(touched days), not O(archive). Days before the cutoff are pruned (files deleted,
rows dropped); observations before the cutoff are dropped and counted (`dropped_before_cutoff`),
never raised. The index is rewritten every run (`generated_utc`), so one small commit per hour
is expected.

## Client (history-window.mjs, DOM-free)

`createLightningHistoryClient({indexUrl, feedUrl, fetchImpl, feedFetchImpl, now, …})`
returns `{index, feed, loadWindow, cacheStats, periods, states}`.

- Windows are UTC, end-anchored, `[start, end)`; `12h 24h 48h 7d 30d 3m 6m 12m custom`;
  `previousUtcWindow`/`nextUtcWindow` navigation; **end clamped to now**; **start clamped to the
  cutoff and flagged** (`clamped.start`), a window entirely before the archive is the
  `BEFORE-ARCHIVE` state, never an exception.
- Accepts **index v1 and v2** (`normaliseIndex`); v1 rows are derived from `scan_windows`.
- Tiers: `≤ 31 d` → detail (default) **only if** the index sum of `bytes ≤ 32 MB` and
  `count ≤ 40 000` (`windowBudget`, decided **before any shard request**); otherwise overview
  with `canLoadPoints`; `loadWindow(w, {consentPoints: true})` loads the newest days up to the
  map cap and reports "N drawn of M archived". `> 31 d` → index only (daily counts).
  `tier: 'feed'` (or automatic fallback when the index is unavailable) → the live 7-day feed in
  one request, with dates the 40 k feed cap removed completed from shards.
- **allSettled** fan-out, 4 concurrent, failures named per day (`failures[{date, path, error}]`),
  index counts still returned; count mismatches are `warnings`.
- **LRU of 8 shards** in memory; an index refresh drops only unsealed or changed shards.
- Coverage: `windowCoverage(index, w)` → searched fraction, runs, intersecting scan range,
  not-yet-ingested tail, before-archive head, dates without a run; `dayCoverage(index, date)`.
- States (`STATES`, `STATE_COLOURS`): `OFF LOADING READY·DETAIL READY·SUMMARY EMPTY-COVERED
  PARTIAL UNKNOWN BEFORE-ARCHIVE FAIL`, with `stale` as a modifier (`classifyState`).
- `captionLines(result)` renders the seven caption templates of the time-window UX
  specification (window, actual, flashes, refresh, searched, warning, footer).

## Verification

```bash
python -m unittest discover -s tests -v      # 49 tests: collector, archive, status
node --test tests/test_history_window.mjs     # 24 tests: client rules
python history.py verify --archive-dir data/history
```

Not verifiable offline: the EUMETSAT download path (`eumdac`, `netCDF4`) and the GitHub
Actions run itself. Both are exercised only by the scheduled job.

## Owner preservation gate

See https://github.com/Ventusltd/gridatlas/issues/28. Production Atlas remains generation
202609241334; this repository changes data only. The GridAtlas implant that consumes this
contract is reviewed and released separately.
