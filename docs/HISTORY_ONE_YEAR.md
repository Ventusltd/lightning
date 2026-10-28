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
  Earlier dates have **no back-data** unless an owner runs the manual **backfill** (below), which
  fills a never-searched date only from a fresh whole-day EUMETSAT search with that search's
  examined slots as its coverage. Nothing is interpolated, estimated or back-dated.
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
   not an empty sky. An **unreadable product** (ZIP without a BODY NetCDF, corrupt ZIP or
   NetCDF) is an *unparsed* slot: not examined, so not searched time; the day becomes partial
   with that slot unknown (never zero) and the run carries on. Its id and reason are in the run
   record (`unparsed_products`, `products_unparsed`, `unparsed_slots_utc`) and the count in
   `data/status.json` (`last_products_unparsed`; `backfill.unparsed_products`). Network, auth,
   rate-limit and timeout errors still stop the run, as does a UTC date with more than 20 % of
   its slots in the query window unparsed (`collect.TooManyUnparsed`; that date stays unknown).
3. Writes the **feed first** (`data/uk_lightning.geojson`, 7-day retention, 40 000 display cap,
   byte-compatible with the previous feed plus additive metadata: `slots_examined`,
   `scan_end_utc`, `published_at_utc`, `first/last_detection_utc`, `coverage_note`).
4. Leaves a **run record** in `$RUNNER_TEMP` (never committed) with the uncapped incoming flashes,
   the previously published feed features (the seed), the examined slots and the searched ranges.

The workflow then commits the **feed on its own**, runs `history.py ingest` as a separate step
(`continue-on-error`), writes `data/status.json` in an **always-run** step (`status.py`), commits
**archive + status separately**, and finally fails the job if collection or archive failed, or
while any `shard_lost` integrity alert is raised (`status.py alert`), so the Actions UI shows red. **An archive failure never blocks the feed.**

`data/status.json` (`uk-lightning-status.v1`): `freshness` (ok / warn > 6 h / stale > 12 h /
stopped > 24 h / never); `health` = `freshness`, except that it is `degraded` instead of ok/warn
while any date is `shard_lost` (the archive is never reported entirely healthy then);
`integrity` `{state: ok|shard_lost|unknown, alert, lost_days, quarantined_observations, source}`
(a failed or skipped archive step carries the previous state, `source: previous_status`);
`last_attempt_utc`, `last_attempt_outcome`, `last_error`,
`consecutive_failures`, `last_successful_scan_start/end_utc`, the last run's lookback, slots,
searched and unsearched ranges, feed counts and the archive outcome, plus the last manual
`backfill` block (see *Backfill* below; written only by the backfill workflow).

`heartbeat` explains staleness: `cron_utc` (`17 * * * *` plus the `47 * * * *` fallback),
`next_expected_run_utc`, `late_after_utc` (last attempt + 90 min), `this_run` (`trigger`,
`run_id`, `run_created_utc`, `job_started_utc`, `queued_minutes`), `gap_before_this_run`
(`previous_attempt_utc`, `minutes`, `hourly_slots_without_a_run`, `cause`: `on_time`,
`github_schedule_late_or_dropped`, `queued_behind_another_run`,
`github_schedule_late_then_queued`, `no_scheduled_run_until_manual_start`, `unknown`), a
one-line `explanation` and an `if_late` sentence for readers who find the heartbeat older than
`late_after_utc` (a run that never started cannot write its own cause; the next run does).

### Schedule and concurrency

GitHub's scheduler does not guarantee scheduled runs. On 27 Oct 2026 the hourly cron (live from
02:38 UTC) produced runs for only 2 of the 13 slots from 03:17 to 15:17 UTC (08:55 and 15:19);
no run was cancelled or skipped by concurrency (the Actions API lists none). The 08:55 run then
waited 91 min queued behind a backfill in the shared group `uk-lightning-update`. Since then:

- the backfill has its own group, `uk-lightning-backfill`; only the hourly workflow uses
  `uk-lightning-update`, whose runs are capped at 55 min, so the feed can only queue behind
  another hourly run, never behind a multi-hour backfill;
- both writers commit with `ci/push-with-replay.sh`: on a rejected push they reset to the new
  `main` and **replay** their own inputs (`ci/replay-hourly-archive.sh` re-runs ingest, verify
  and the status write from the run record; `ci/replay-backfill.sh` re-ingests each filled date
  from its saved whole-day record, dropping a date the hourly job has meanwhile searched). There
  is no textual `git pull --rebase` of `index.json` or `status.json`;
- a fallback cron slot at :47 runs a gate job (`status.py due`) that skips when a run succeeded
  less than 40 min earlier, so one dropped :17 slot costs about 30 min. Longer scheduler outages
  can still happen; only an external trigger (a `workflow_dispatch` call from outside GitHub's
  scheduler) removes that dependency.

## Archive layout

```
data/history/index.json                 uk-lightning-history-index.v2
data/history/days/YYYY/MM/DD.json       uk-lightning-history-day.v1 (one UTC day)
data/history/days/YYYY/MM/DD/HH.json    hourly parts of a day above the 8 MiB target
data/history/quarantine/YYYY-MM-DD.json flashes seen for a shard_lost date (NOT counted)
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
  the row becomes `coverage none`, `scanned_seconds 0`, `count null` (unknown, never zero) and
  stays so on later runs until `history.py recover` (below) or the date leaves retention.
  Remaining files are left on disk, unlisted. See *Lost shards* below.
- `integrity` (top level): `{state: "ok" | "shard_lost", alert, lost_days[], recoveries[]}`.
  Each `lost_days[]` entry keeps `date`, `reason`, `detected_utc`, `missing_paths`,
  `quarantined_observations`, `quarantine_path` and `last_recorded` (the last index entry for
  the date: `path`/`parts`, `count`, `bytes`, `sha256`, `index_generated_utc`), which is what a
  recovery must match. `alert` is a plain-language `INTEGRITY ALERT: …` string, `null` when ok.
  `verify` rejects an index whose `shard_lost` rows and `integrity.lost_days` disagree, so the
  flag cannot be cleared by hand-editing one of them.

### Lost shards: alert, quarantine and recovery

1. **Unknown, never zero.** The row is `coverage none / count null`; clients
   (`windowCoverage`) do not count that date's scan-window seconds as searched and name it in
   `lostDays` and the warning caption (`Integrity: … count unknown, not zero`).
2. **Recorded and alerted.** `index.integrity` and `status.json` `integrity` carry the alert;
   `health` becomes `degraded`; `history.py ingest/verify` print it to stderr; the workflow's
   last step fails the run (after the commits) while it is raised.
3. **Healthy days continue.** Other dates are ingested, verified and committed as usual. Flashes
   arriving later for the lost date are **not** counted and **not** discarded: they are held in
   `quarantine/YYYY-MM-DD.json` (idempotent by id; only if that file would exceed
   `max_shard_bytes` are new ones dropped and counted in `dropped_lost_day`).
4. **Recovery only from verified evidence** — `history.py recover --day YYYY-MM-DD` with exactly
   one source; every gate runs before anything is written, and a refusal changes nothing:
   - `--from-shard FILE [FILE …]`: earlier copies of the lost file(s), e.g.
     `git show <commit>:data/history/days/2026/10/24.json > /tmp/24.json`. Each file's bytes and
     sha256 must equal a recorded entry, its feature count must equal the recorded count, every
     recorded part must be supplied or still present with its recorded sha256, and no unlisted
     file for the date may remain. If the lost entry has no recorded evidence (flagged before it
     was kept), add `--recorded-index` with an earlier `index.json` from git history.
   - `--rebuild --rebuild-record RECORD`: a fresh, independent EUMETSAT search of the date made
     with `python collect.py --rebuild-day YYYY-MM-DD --run-record RECORD` (writes only the run
     record, never the feed or status). Gates: successful run with `slots_parsed: true`; `run_utc`
     after the loss was detected; examined slots cover every second the index recorded as
     searched on that date (the whole date if none was recorded); it contains every flash still
     on disk or in quarantine; and its flash count equals the recorded count. With no recorded
     count the rebuild is refused unless `--accept-unanchored` is given (audited
     `anchored: false`). A partial or different count is never accepted.
   On success the verified files are written, quarantined flashes are merged on top, the row is
   restored, the date leaves `integrity.lost_days`, an audit record is appended to
   `integrity.recoveries` (`method`, `verified_against`, input sha256s, `byte_identical`,
   `quarantined_merged`, result counts/hashes, and for a rebuild the covered seconds), and
   `verify` runs. Commit `data/history` by hand after reviewing the audit.

### Backfill of never-searched dates (manual workflow)

`.github/workflows/backfill-lightning.yml` is **`workflow_dispatch` only** (no schedule). Inputs:
`end_date` (newest UTC date, empty = yesterday UTC; must be before today), `days` (dates to
consider back from `end_date`, default 7, **1–31**), `dry_run` (only print the plan: no EUMETSAT
call, no commit). It has its **own** concurrency group **`uk-lightning-backfill`**
(`cancel-in-progress: false`): two backfills never overlap, and the hourly job is never queued
behind one (see *Schedule and concurrency*). Writing runs must be on `main`.

1. **Plan** — `history.py backfill-plan` lists the dates in `[end_date − days + 1, end_date]`,
   newest first, whose row is `coverage none / count null` and **not `shard_lost`** (those need
   `recover`), stopping at the retention cutoff. It refuses when no `index.json` exists: the
   hourly job creates the archive and merges the one-off seed windows first.
2. **Search** each date with `python collect.py --rebuild-day D --run-record $RUNNER_TEMP/rb-D.json`:
   an independent search of the whole UTC date that writes only the run record (never the feed
   or status).
3. **Ingest** with `history.py backfill --day D --rebuild-record R` (distinct from `recover`,
   which is for `shard_lost` dates and must match a recorded count). Gates, all before any write:
   the index exists; the row is never searched and not `shard_lost`; the record is a successful
   `uk-lightning-run-record.v1` with `products_examined ≥ 1`, `slots_parsed: true`,
   `rebuild_day_utc == D`, `query_start/end` = the whole date and `run_utc` after the date ended;
   every scanned range lies inside D; every flash is valid and comes from an **examined
   product**; no unlisted shard files exist for D. A flash's product (the nominal
   `_OPE_<start>_<end>_` in its id, `collect.flash_slot`) only decides **whether** it was
   examined: that product must be among `slots_examined_utc`, else the record is refused. The
   flash's **own timestamp** decides its UTC date and is checked against examined time
   (`collect.classify_flash`). It is consistent when it lies within `SLOT_EDGE_TOLERANCE_SECONDS`
   (60 s) of its product's name span, or inside **any** examined span. A product's name span
   does not bound its flashes' times. Products that start late in their 10-minute cycle (e.g.
   `..._OPE_20260412001255_20260412002005_..._O_0002_...`, cycle 00:10-00:20) or right after
   midnight (`..._OPE_20260828000049_20260828001009_..._O_0001_...`) held flashes more than
   60 s outside it. Runs 40620556193 and 40657338115 refused those whole dates. A flash of an
   examined product timed in time **no** examined product covers is **kept and counted**. Its
   10-minute slot stays unsearched, so the date is `partial` there: never zero, never refused.
   The result lists it as `unexamined_time_flashes` / `unexamined_time_slots_utc`. One flash
   listed by two products (same `time_utc` and position) is archived once, as
   `cross_product_duplicates`. A flash of an unexamined product, or a legacy id without a
   product slot outside every examined span, is
   still refused. A valid flash timed on the **neighbouring UTC date** (a product straddling
   midnight) is dropped from D and counted as `outside_day` (`collect.py --rebuild-day` drops and
   counts them already; the record carries `outside_day`), never refused. The ingest then runs through the ordinary
   `archive_observations` path (same dedupe, sorting, 8 MiB hourly split and **25 000 000-byte
   `max_shard_bytes` hard cap**) and `verify` on a **staging copy** that replaces `data/history`
   only when both succeed and the new row's `scanned_seconds` equals the record's examined
   seconds and its `count` equals the record's flashes. Any refusal or failure (including
   `ShardTooLarge`) changes nothing. The row becomes `partial` or `full` with the **real
   examined coverage**: slots EUMETSAT did not return stay unsearched, and `count 0` appears only
   when the searched time had no flash. The index keeps the hourly job's `last_run_lookback_hours`;
   `last_successful_scan_end_utc` does not move back.
4. **Stop cleanly.** The first `--rebuild-day` failure (EUMETSAT error, HTTP 429 rate limit,
   outage, or "No EUMETSAT product files returned") stops the loop (`eumetsat_error`), as does
   an ingest refusal (`archive_refused`) or the soft time budget (`BACKFILL_BUDGET_MINUTES`, 200,
   inside the 300-minute job timeout; `time_budget`). Remaining dates stay never searched
   (unknown, not zero); run the workflow again later.
5. **Status, verify, one commit.** `status.py backfill` writes a `backfill` block into
   `data/status.json` (`last_run_utc`, `outcome` completed / time_budget / stopped,
   `stopped_reason`, `error`, `planned_days`, `filled_days`, `remaining_unknown_days`) and
   refreshes `archive.archived_observations`, `archive.index_generated_utc` and `integrity` from
   the new index. It **never touches the hourly heartbeat** (`written_utc`, `last_attempt_*`,
   `consecutive_failures`, `last_successful_scan_*`, `freshness`), so a backfill cannot move the
   lookback anchor; the hourly `status.py write` carries the `backfill` block forward. Then
   `history.py verify` runs and `data/history` + `data/status.json` are committed in **one
   commit** per run (replayed onto a newer `main` if the hourly job pushed meanwhile). An `eumetsat_error` or `archive_refused` stop turns the run red after that
   commit; a `time_budget` stop only warns.

**Does EUMETSAT's Data Store keep past MTG LI L2-LFL products? Unverified.** Checked 27 Oct 2026
against EUMETSAT's own pages: the LI Level-2 operational dissemination validation report
(30 Oct 2024) plans operational dissemination of LI L2 products, flashes included, from the
beginning of November 2024 (AERIS, a secondary source, dates the operational declaration to
31 Oct 2024). EUMETSAT
describes the Data Store as "a growing catalogue" that gradually replaces the Data Centre's
data service, with the Data Centre focused on long-term preservation. None of the pages
reachable here states the temporal extent or a retention/rolling window for
`EO:EUM:DAT:0691`, and its catalogue page could not be fetched from this environment. So
whether past dates (up to the one-year cutoff) are still searchable is **unverified**. The
workflow does not depend on it: a date with no returned product fails `--rebuild-day` and
stops the run with the date left unknown. A `dry_run`, then a one-day run (`days: 1`) on an old
`end_date`, is the cheapest empirical check.

### Parallel backfill (manual workflow)

One whole-day `--rebuild-day` search takes about **18.5 minutes** (run 40464739813: 11 dates in
203 minutes), so the sequential workflow fills about 10 dates per run. The rest of the year is
about 337 dates, which would take about 34 more runs. `.github/workflows/backfill-parallel-lightning.yml` splits the same work. It is
**`workflow_dispatch` only**, with the inputs `end_date`, `days` (**1–366**, default 90), `shards` (**1–6**, default 6) and
`dry_run`. The steps run through `backfill_parallel.py` and use the same gates as above:

1. **plan** job: `backfill_parallel.py plan` lists the never-searched dates
   (`history.backfill_plan`, newest first, `shard_lost` excluded) and deals them **round-robin**
   into at most six shards, each newest first. Writing runs are refused outside `main`.
2. **shard** matrix (`max-parallel: 6`, the EUMETSAT fair-use cap). Each shard runs
   `collect.py --rebuild-day D` for each of its dates and writes **only run records**, never the
   feed, the status or the archive. A record is moved into place only after `collect.py`
   succeeds, so a killed search leaves no partial record. A `shard-K.json` summary (dates,
   collected records with their sha256, stop reason) is rewritten after every date. Each shard
   waits 20 s × (K − 1) before its first search. No new date starts after
   `SHARD_BUDGET_MINUTES` (170). A date still running at `SHARD_HARD_STOP_MINUTES` (190), or
   after 45 minutes on its own, is killed and left unknown (`time_budget`, a warning); the search
   step has a 195-minute backstop and the job a 200-minute timeout, so the records always upload
   as the artifact `backfill-shard-K` (`if: always()`, 7 days). Every HTTP call made by
   `collect.py` (token, search, product download) has an explicit (connect, read) timeout of
   (15, 60) s, because eumdac passes none: a stalled read raises a transient read timeout, which
   is retried, and a product still timing out is UNPARSED (the per-day unparsed threshold still
   applies). Run 40564980740's shard 3 hung for over 5 h on such a read and held finalize for
   every shard.
3. **Stop cleanly.** The first EUMETSAT error in a shard (HTTP 429, outage, no products) stops
   that shard, which uploads its records and then fails. `fail-fast` then cancels the other
   shards. A cancelled shard writes `cancelled`, deletes its partial record and still uploads
   what it finished.
4. **finalize** job (`if: always()`, the only job with `contents: write`). It downloads all the
   artifacts, waits (at most 20 min, best effort) while an `update-lightning.yml` run is active,
   resets to the newest `origin/main` and ingests the records **sequentially**, newest first,
   with `history.backfill_day`. A record whose sha256 differs from its shard summary is refused.
   A date that is no longer never searched is skipped, not refused, so the step is idempotent.
   A refused date (a gate of `history.backfill_day`, a record changed in transit, or any other
   error while ingesting it) is **skipped only**: it stays never searched (unknown, not zero),
   its reason is recorded in `refused_days`, and every other date is still ingested and
   committed, so one date can never cost the whole run. Then `status.py`'s `backfill` block is
   written with `mode: parallel`, `shards`, `skipped_already_searched_days`, `refused_days`
   (date → reason), `outside_day_by_day`, `unexamined_time_flashes_by_day` and
   `cross_product_duplicates_by_day`, the hourly heartbeat is left
   untouched, `history.py verify` runs and the job makes **one commit**. If the push is rejected
   because the hourly job committed meanwhile, the job resets to the new main and re-ingests
   from the same records, up to 5 attempts. It never rebases `index.json`.
5. **Surface.** `eumetsat_error`, `archive_refused`, `cancelled` or `shard_missing` turns the
   run red after the commit. `time_budget` only warns. Dates without a verified record stay
   never searched (unknown, not zero).

**Concurrency.** The workflow has its own group, `uk-lightning-backfill`, and never uses
`uk-lightning-update`, so the hourly collector is never queued behind it, and a pending hourly
run can never be replaced by a pending backfill run, or the reverse. Only the finalize job
pushes, and it takes seconds. Expected throughput is about 3.2 dates per shard-hour: about
**19 dates per wall-clock hour with 6 shards**, about 90 dates per run, and about 4 runs for
the rest of the year. This assumes EUMETSAT serves six parallel searches as fast as one, which
is not yet measured. Start with `days: 30`, `shards: 3`.

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

### Month density grid (`uk-lightning-history-grid.v1`, `grid/YYYY-MM.json`)

`grid.py` derives one file per UTC calendar month from the day shards: archived **optical** flash
counts per 0.1° cell (≈ 7 × 11 km; `cell = row*140 + col`, `col = floor((lon+11)/0.1)`,
`row = floor((lat-49.5)/0.1)`, clamped) per UTC day, as `cells: [[cell, day, n, day, n, …], …]`.
Any UTC-day window sums exactly from at most 13 files. `days` lists every retention date of the
month with the index row's `coverage`, `count` (null = never searched: unknown, not zero, no
cells) and the shard `sha256` list it was built from; a client must treat a day whose entry
differs from the current index row as stale (unknown). Each day's cells sum to its `count`.
The file has no timestamp, so it is byte-identical until its month changes.

`archive_observations` refreshes the grids after writing the index, so the hourly ingest, the
backfills (inside their staging copy) and `recover` all keep them current; only months whose rows
changed are rewritten and only changed days are re-read. A grid failure never blocks the archive
(reported as `grid.error`; clients then see stale days as unknown). One-off/repair generator:
`python grid.py build --archive-dir data/history`; check: `python grid.py verify --deep`.
Measured on 2026-10-28 (363 531 flashes, 157 days with flashes): 13 files, 320 849 bytes in
total (≈ 100 kB gzip), largest 135 113 bytes (2026-06); a full rebuild takes ≈ 1.5 s, a no-op
refresh ≈ 0.1 s.

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
python -m unittest discover -s tests -v      # collector, archive, status, workflow shell, shard_lost, backfill (fake downloader)
node --test tests/test_history_window.mjs     # client rules
python history.py verify --archive-dir data/history
```

Not verifiable offline: the EUMETSAT download path (`eumdac`, `netCDF4`) and the GitHub
Actions run itself. Both are exercised only by the scheduled job.

## Owner preservation gate

See https://github.com/Ventusltd/gridatlas/issues/28. Production Atlas remains generation
202609241334; this repository changes data only. The GridAtlas implant that consumes this
contract is reviewed and released separately.
