# Methods: UK optical lightning flash archive

This document describes how `data/history` is produced, what its numbers mean and how
anyone can recompute or re-download them. It is the methods note for the archive behind the
GridAtlas lightning overlay. The engineering detail of the collector, the archive and the
date-window client is in [`HISTORY_ONE_YEAR.md`](HISTORY_ONE_YEAR.md). This note does not
replace it.

Four statements hold throughout:

- **An optical flash is not a ground strike.** The archive records flashes seen from orbit,
  including intracloud lightning. It never shows that a particular place was struck.
- **Unknown is not zero.** Time that was not searched has no count. It is never shown as 0.
- **Proximity is not electrical exposure.** A flash near an asset says nothing about a current
  path, an attachment point or induced voltages.
- **This is exploratory data, not a lightning risk assessment.** It is not a ground flash
  density in the sense of IEC 62305-2 (see *Known limitations*).

## 1. Source product

| item | value |
|---|---|
| Product | EUMETSAT Meteosat Third Generation (MTG) Lightning Imager (LI) Level 2 **Lightning Flashes (LFL)** |
| Collection | `EO:EUM:DAT:0691` ("LI Lightning Flashes - MTG 0 degree" in `collect.py`), <https://api.eumetsat.int/data/browse/collections/EO%3AEUM%3ADAT%3A0691?format=html> |
| Access | EUMETSAT Data Store through the `eumdac` client (`requirements.txt`), with a consumer key and secret (`EUMETSAT_CONSUMER_KEY` and `EUMETSAT_CONSUMER_SECRET`) |
| Data policy | Listed as `NoConditions` when the collector was written. Attribution is kept in every GeoJSON (`metadata.license`). Re-check the collection page before reuse: the policy has not been verified again for this note. |
| Read | Each product is a ZIP. Every `*BODY*.nc` NetCDF inside it is read for the `longitude`, `latitude` and `flash_time` variables, with CF units and calendar (`collect.decode_body`). Masked values are skipped. |
| One record | One flash. `id` = `<product name>:<index>` (a second BODY file in the same ZIP gives `<product>:b<k>.<index>`). Properties: `time_utc` (milliseconds), `source_product: "MTG LI L2-LFL"`, `observation_kind: "optical_lightning_flash"`. Coordinates are rounded to 6 decimal places. |

The LI is an optical instrument in geostationary orbit. It detects lightning flashes of every
kind (intracloud, cloud-to-cloud and cloud-to-ground) by their light at cloud top, and it does
not classify cloud-to-ground strokes. The archive keeps only the fields listed above. It does
not keep flash energy, duration, footprint or quality flags.

### Only fully open data is used

Every number in this archive comes from one source: the EUMETSAT LFL product above, read
through the public Data Store. Nothing else is ingested, blended or used for calibration.
Independent sources are used only to **check** the archive, and then only if their terms allow
this use without a contract. An independent cross-check (29 Oct 2026, archive at `eb99b28`)
reviewed the candidates:

| source | terms, as reviewed | use here |
|---|---|---|
| EUMETSAT MTG LI L2-LFL (`EO:EUM:DAT:0691`) | free Data Store account; listed `NoConditions` | **the only data source** |
| Met Office Monthly Weather Reports, blogs and public statements | free to read; dates and facts may be quoted (Crown copyright; maps not reproduced) | qualitative date and region checks only |
| Met Office LEELA on WMO WIS2 | real-time "core data", no conditions on use, credit requested; **no open archive** (history is sold) | not used yet; a prospective open comparison is possible |
| Published UK climatology (Hayward et al. 2022) | copyright paper; figures may be cited | season and region sanity check only |
| Blitzortung.org | terms exclude "risk analysis for precautionary protection of high-quality technology", storm warning and commercial interests | **not used, and must not be** |
| ESWD, EUCLID | signed agreement, or approval of every member | not used |
| Vaisala GLD360 / Xweather, Earth Networks ENTLN | commercial | not used |
| NASA ISS-LIS | open, but the record ended in late 2023 and reached only 52° N | no overlap |

The cross-check found that all 8 dated public accounts of thunder that it located (for
example 9, 22 and 25–27 June 2026, and 26–27 May 2026) match archive activity on the right UTC
date and in a broadly correct region. This is a **qualitative** agreement. Because no fully
open, quantitative reference archive exists for the period, detection efficiency, false
alarms and location accuracy over the UK remain **unvalidated** (see *Known limitations*). A
Met Office figure of 29 074 strikes in the 24 h to 09:00 BST on 23 June 2026 compares with
8 630 archived flashes in the same window. Strokes and optical flashes are different
quantities, so that ratio is not a detection efficiency.

## 2. Search windows

A flash enters the archive only through a search of the Data Store. There are three kinds of
search:

1. **Hourly collector** (`.github/workflows/update-lightning.yml`, cron `17 * * * *` plus a `:47`
   fallback). It searches `[now − L, now)` with `L = max(6 h, now − last successful scan end +
   30 min)`, capped at 48 h (`collect.lookback_window`). A failed or late run is therefore
   caught up by the next one. A gap older than 48 h cannot be reached by the schedule and stays
   unknown.
2. **Backfill** (`backfill-lightning.yml`, and `backfill-parallel-lightning.yml` with up to six
   date shards). For one **whole UTC date** it runs `collect.py --rebuild-day YYYY-MM-DD`, which
   searches `[D 00:00, D+1 00:00)` and writes only a run record. `history.py backfill` then
   ingests that record if every gate passes. A backfill fills **only** a date that was never
   searched.
3. **Seed.** The first archive run (27 Oct 2026) took the flashes already in the 7-day live
   feed. Their search windows were reconstructed from the git history of the feed
   (`data/seed_scan_windows.json`). Those days were only partly searched.

The archive covers one calendar year of UTC dates: `cutoff_date_utc` (the same date one year
before `generated_utc`) through today. Older days are pruned, so **the archive and every
total in it roll forward each day.** Always quote a commit SHA with a number.

## 3. Slots, examined time and the ±60 s rule

**Slots.** LFL products are 10-minute sensing slots, 144 per UTC day. Each product name carries
its nominal sensing start and end: `…_OPE_<YYYYMMDDHHMMSS>_<YYYYMMDDHHMMSS>_…`
(`collect.PRODUCT_SLOT`). The phase is a few seconds past the 10-minute mark (`:05` and `:09`
are both seen).

**Searched time** is the union of the name spans of the products that were actually
**examined** (downloaded and decoded), clipped to the query window (`collect.scanned_ranges`).
It is not the query window. The archive stores the merged union as `index.scan_windows`, and
`scanned_seconds` for a date is that union intersected with the date. The following are not
searched time:

- slots the Data Store did not return (`unscanned_ranges`);
- the unpublished tail of a query window (`tail_unobserved_seconds`);
- **unparsed** products: a corrupt ZIP or NetCDF, a ZIP without a BODY file, or a download that
  still fails after its retries (HTTP 400/404/409/410/416/422/423, or a persistent timeout).
  Their slots are listed in the run record (`unparsed_slots_utc`). A date with more than 20 %
  of its slots in the window unparsed is not recorded at all, and it stays unknown
  (`collect.TooManyUnparsed`).

**Which date a flash belongs to.** The flash's **own `time_utc`** decides its UTC date. A
whole-day search drops flashes of a product that straddles midnight if they are timed on the
neighbouring date, and counts them as `outside_day`.

**The ±60 s rule and the PR #13 fix.** A product's name span does not bound the times of the
flashes inside it. Real products hold flashes about 0.4 s outside their span. Products that
start late in their cycle, or just after midnight, hold flashes more than 60 s outside it.

- **Before PR #13**, the backfill gate (PR #12) required every flash to lie within ±60 s
  (`SLOT_EDGE_TOLERANCE_SECONDS`) of its own product's name span. One flash beyond that
  refused the **whole date**. Backfill runs 40620556193 and 40657338115 refused 2026-04-12
  (`…_OPE_20260412001255_20260412002005_…_O_0002_…:2326`) and 2026-08-28
  (`…_OPE_20260828000049_20260828001009_…_O_0001_…:203`). The decoder also silently dropped
  flashes outside the product span ±60 s.
- **PR #13** ([Ventusltd/lightning#13](https://github.com/Ventusltd/lightning/pull/13), merged
  28 Oct 2026 09:40 UTC, merge commit `320efa7`) separates two questions:
  - The **product** says **whether** a flash was examined: its nominal start must be among the
    examined slots, or the record is refused.
  - The flash's **own time** says **when** it happened (`collect.classify_flash`). The time is
    consistent if it lies within ±60 s of its product's span (`in_product_span`) or inside
    **any** examined span (`in_examined_span`).
  - A flash of an examined product timed in time that **no** examined product covers is **kept
    and counted** (`in_unexamined_time`). Its slot stays unsearched, so the date is `partial`
    there.
  - One flash listed by two products (the same `time_utc` and position) is archived once
    (`cross_product_duplicates`).
  - Every flash of a product is now decoded. Flashes outside the window or date are dropped
    **and counted**, never lost silently.

  After the fix, 2026-04-12 (48 flashes) and 2026-08-28 (3,617 flashes) were filled. Both are
  `partial`.

## 4. Geographic bounds

`UK_BOUNDS = (−11.0, 49.5, 3.0, 61.5)` as (west, south, east, north), in degrees on WGS 84,
**inclusive**. This is a **rectangle, not the UK border**. It contains all of Ireland, parts of
northern France and Belgium, and the surrounding seas. A flash is kept if its decoded
longitude and latitude are finite and lie inside the rectangle (`collect.inside_uk_view`). No
land mask is applied in this repository.

## 5. Coverage semantics

Every retained UTC date has exactly one index row (`index.json`, `uk-lightning-history-index.v2`):

| `coverage` | meaning | `count` |
|---|---|---|
| `full` | every second of the date lies in an examined slot (`scanned_seconds` = 86 400) | number of archived flashes (may be 0) |
| `partial` | some of the date was examined (`0 < scanned_seconds < 86 400`) | flashes in the **searched part only**: a lower bound for the day |
| `none` | never searched, or `shard_lost` | **`null`, meaning unknown, never 0** |

The rules, which are checked by `history.py verify` and independently by `reproduce.py`:

- `count` is `null` if and only if `coverage` is `none`;
- `count: 0` appears only for a date with searched time;
- a date with archived flashes but no recorded window becomes `partial`, with
  `scanned_seconds` = the 10-minute slots that its flashes' products prove were examined (a
  lower bound; seed days only);
- `sealed` means the shard can no longer change: the date is `full`, or 48 h have passed since
  it ended.

**What the 12-month total means.** The archive total is the number of optical flashes
**archived in searched time**. It is not an annual flash count. It omits flashes in
unsearched time and flashes the instrument did not detect (see *Known limitations*). Do not
scale it up by the searched fraction: lightning is very bursty, and the gaps are not random
in time.

## 6. Integrity

- **Per-file digests.** Each index row records `bytes` and `sha256` for its day shard, or for
  each of its hourly parts. A day is split into hourly parts when its whole file would exceed
  8 MiB (`target_shard_bytes`). Any single file above 25 000 000 bytes (`max_shard_bytes`)
  fails the run. Files are written byte-stably: compact JSON, UTF-8, one trailing newline,
  features sorted by `(time_utc, id)` and deduplicated by `id`. Rewriting identical content
  therefore gives identical bytes.
- **Verification.** `history.py verify` checks every listed file against its row, and
  `grid.py verify --deep` recounts every grid from the shards. Both run before every data push
  and again on `main` (`verify-lightning-data.yml`). `reproduce.py check` repeats all of this
  without importing the writer's code (section 8).
- **`shard_lost`.** If a file listed by the index disappears, the date becomes `coverage:
  none`, `count: null` and `shard_lost: true`. It stays so until `history.py recover`
  restores it. Recovery needs either byte-identical earlier copies, whose sha256 and count must
  equal the recorded ones, or a fresh whole-day search whose count equals the recorded count.
  Flashes that arrive later for a lost date are held in `quarantine/` and not counted.
  `index.integrity` and `status.json` raise an `INTEGRITY ALERT`, `health` becomes `degraded`,
  and the workflow turns red. On the commit recorded below no date is `shard_lost`.

## 7. Month grids

`data/history/grid/YYYY-MM.json` (`uk-lightning-history-grid.v1`) gives, for each UTC day,
the number of archived optical flashes in each **0.1° × 0.1° cell** of the bounds rectangle:
140 columns × 120 rows.

- `col = floor((lon + 11) / 0.1)` and `row = floor((lat − 49.5) / 0.1)`. The quotient is rounded
  to 6 decimals first, so an exact 0.1° step never falls into the cell below. Values are
  clamped to the grid, so a flash on the east or north edge falls in the last column or row.
  `cell = row × 140 + col`.
- A cell is **not** a 10 km square. 0.1° of latitude is about 11.1 km. 0.1° of longitude is
  about 7.2 km at 49.5° N, 6.4 km at 55° N and 5.3 km at 61.5° N. A cell's centre is not a flash
  location.
- Each day's cells sum exactly to its index `count`. A day with `count: null` contributes no
  cells (unknown, not zero). Each grid day carries the index row's `coverage`, `count` and
  shard `sha256` list. A client must treat a grid day that differs from the current index as
  stale, which means unknown.
- The grid is a pure function of the shards and index rows: it holds no timestamp.
- GridAtlas may draw only a subset of cells, for example within a set distance of UK land.
  That is a display choice in GridAtlas, not a property of this data. A masked map is not an
  unbiased national density map.

## 8. Reproduce from the committed files

`reproduce.py` uses only the Python standard library. It needs no network or credentials and
imports none of `collect.py`, `history.py` or `grid.py`. It re-implements the rules above, so
an error in the writer is not repeated silently by the checker.

```bash
git clone https://github.com/Ventusltd/lightning && cd lightning
git checkout <commit>
python3 reproduce.py check --json reproduction.json     # exit 0 = every value reproduced
```

It does the following:

1. It hashes every file under `data/history` and derives `manifest_sha256`, the sha256 of the
   sorted `<sha256>  <path>` lines.
2. It re-reads every day shard and hourly part. It checks the schema, date, metadata and
   feature validity (an optical-flash Point inside the bounds, on its own UTC date and in its
   own hour for a part). It checks that ids are unique, that the order is canonical, that the
   bytes are the canonical serialisation, and that the 8 MiB split rule and the 25 MB cap hold.
3. It recomputes every index row: `scanned_seconds` from `scan_windows`, `coverage`, `count`,
   `bytes`, `sha256`, `path` or `parts`, first and last observation, `sealed` and
   `shard_lost`. It also recomputes every index total and the integrity bookkeeping.
4. It rebuilds every month grid from the shards and compares it byte for byte with the
   committed file.
5. It derives the headline numbers over all retained dates.

The output ends with `manifest_sha256`, `index_sha256` and `summary_sha256`.
`summary_sha256` is the sha256 of the canonical JSON summary, excluding the wall-clock `run`
block. Anyone who runs the check on the same commit gets the same digest.
`--expect <summary_sha256>` fails on any difference. The digest is a content hash, not an
identity signature. To sign a reproduction as yourself, use an SSH key:

```bash
ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n lightning-reproduction reproduction.json   # writes reproduction.json.sig
ssh-keygen -Y verify -f allowed_signers -I you@example.org -n lightning-reproduction -s reproduction.json.sig < reproduction.json
```

### Recorded run (29 Oct 2026)

`python3 reproduce.py check` on `main` at **`eb99b28`** ("data: archive UTC day shards, index v2
and collector status"; index `generated_utc` 2026-10-28T23:32:49.194Z), Python 3.13:

| quantity | value |
|---|---|
| verdict | **reproduced, 0 mismatches** (366 index rows, 374 shard files, 13 month grids, 260 scan windows, 388 files, 196 842 069 bytes) |
| window | UTC dates 2025-10-28 to 2026-10-28; the last date was still being collected |
| days searched | **366 of 366**: 249 full, **117 partial**, 0 never searched, 0 `shard_lost` |
| optical flashes | **372 735** on 262 days; 104 searched days had none |
| searched time | 8 627.37 h = **98.22 %** of the window; **156.63 h unsearched (unknown, not zero)** |
| partial days | median 18.5 min unsearched; 29 days with 1 h or more unsearched. 2026-10-20 had only 1.3 h searched, so its `count: 0` covers 1.3 h, not a day. 2026-10-28 was in progress. |
| concentration | 2026-06-25 to 06-27: 184 938 flashes (49.6 % of the total); busiest day 2026-06-26 with 91 146 flashes. May to August hold 361 562 flashes (97.0 %). |
| split days | 2026-05-27, 2026-06-25, 2026-06-26, 2026-06-27, 2026-08-27 |
| duplicate checks | 0 flashes with the same time and position under another product; 0 ids without a product slot |
| `manifest_sha256` | `043a0727025e430ee6d1759e4c632aa0076bca16b032b0c0c88c7304ecbf0ebe` |
| `index_sha256` | `4a9217b4cd05b4fc7096e11aad64341672e57ea0864a5f621b6948e9b607ba2b` |
| `summary_sha256` | `5d7f6ddf91c8d066f5c202997cf01c748b8a869b4157980b5d473eee27d2c8d4` |

Monthly flashes over searched time: 2025-10 (4 days) 1 200; 11: 1 233; 12: 1 271; 2026-01: 410;
02: 609; 03: 1 244; 04: 1 432; 05: 65 194; 06: 234 642; 07: 3 338; 08: 58 388; 09: 2 904;
10 (28 days) 870.

`main` gains a data commit every hour, so a later run gives other digests. Compare like with
like: check out `eb99b28` to reproduce the table above.

### Re-download a day from EUMETSAT and compare

You need your own free EUMETSAT Data Store account (<https://data.eumetsat.int/>) and its API
consumer key and secret. Never commit them.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt                       # eumdac, netCDF4
export EUMETSAT_CONSUMER_KEY=…  EUMETSAT_CONSUMER_SECRET=…
python collect.py --rebuild-day 2026-06-02 --run-record /tmp/rb-2026-06-02.json
python reproduce.py compare-day --day 2026-06-02 --record /tmp/rb-2026-06-02.json
```

`--rebuild-day` searches the whole UTC date and writes **only** the run record. It never writes
the feed, the status or the archive, and a full day takes about 15 to 20 minutes.
`compare-day` matches flashes on `(time_utc to the millisecond, lon, lat to 6 dp)` and reports
one of these verdicts:

- `identical`: the same flashes and the same searched seconds;
- `same_flashes_different_coverage`;
- `differs`, with counts and samples of the flashes found on only one side;
- `archive_unknown`: the archive never searched that date, so there is nothing to compare,
  which does not mean zero.

`compare-day` never changes the archive. Expect these legitimate differences:

- An hourly-collected date is the union of several runs. A product that was missing or
  unparsed then may be present now, and the reverse.
- EUMETSAT may reprocess products.
- **Whether the Data Store keeps every past LFL product for a full year is unverified.** The
  2025 backfills succeeded, but no retention statement for `EO:EUM:DAT:0691` has been found.

A useful audit is one `full` backfilled date, one `full` hourly date and one `partial` date.

## 9. Known limitations

1. **Optical flashes, not ground strikes.** LI flashes include intracloud and cloud-to-cloud
   lightning, and the product does not separate cloud-to-ground strokes. Counts are therefore
   not a ground flash density (N_G), which IEC 62305-2 risk assessment needs. Converting them
   would need a cloud-to-ground fraction and a detection-efficiency correction, and this
   archive provides neither.
2. **Detection efficiency is below 100 % and varies.** Viticchié et al. (2026), *Meteosat Third
   Generation Lightning Imager Detection Performances*, AMT 19, 6625–6642,
   <https://doi.org/10.5194/amt-19-6625-2026> (preprint
   <https://egusphere.copernicus.org/preprints/2026/egusphere-2026-3177/>), report an average
   flash detection efficiency of 87.4 %: about 70 % by day and 95 % by night. The reference
   networks were GLD360, EUCLID and GLM. This archive applies **no** correction. The figures
   are full-disc results and have not been validated for the UK rectangle.
3. **Location is approximate.** Positions are LI pixel-based geolocations seen obliquely from
   0° longitude at 50–60° N. Parallax and pixel footprint make them uncertain by kilometres.
   The exact magnitude over the UK is **unverified** here. Six-decimal coordinates do not mean
   centimetre accuracy.
4. **Unsearched time.** In the recorded run 156.6 h (1.8 %) of the year was not searched,
   spread over 117 partial days. Partial-day counts are lower bounds. Today's date is always
   incomplete.
5. **Provenance of examined slots.** The index keeps only the **merged** search windows. The
   per-run lists of examined and unparsed products live in run records, which are not
   committed. They are summarised in `data/status.json` for the last run and the last
   backfill, and in that file's git history. The committed files can show *that* a second was
   searched, but not *which product* covered it.
6. **One year, one season of each.** The total is dominated by a few convective days. Three
   days in June 2026 hold half of it. One year is not a climatology.
7. **Rolling retention.** Every total changes daily as old days are pruned and new ones are
   added. Quote the commit.
8. **Rectangle, not a border.** The bounds include Ireland, northern France and Belgium, and
   the sea. A per-country total needs a land mask that this repository does not apply.
9. **Grid cells** are 0.1° in latitude and longitude. They are neither square nor equal in
   area, and edge flashes are clamped into the outer cells.
10. **No independent quantitative reference.** Only fully open sources are used (section 1).
    None of them offers a quantitative archive for the period, so no count-for-count
    comparison with a ground-based network (LEELA/ATDnet, EUCLID, GLD360) has been made. The
    checks so far are qualitative (dates and regions of reported thunderstorms).

## 10. How to cite

Cite the commit you used, the source product and these caveats. For example:

> Ventus Ltd (2026). *UK optical lightning flash archive from EUMETSAT MTG LI L2-LFL
> (EO:EUM:DAT:0691)*, one-year UTC archive, GitHub Ventusltd/lightning, commit `<sha>`,
> `data/history/index.json` generated `<generated_utc>`, reproduction `summary_sha256 <digest>`.
> https://github.com/Ventusltd/lightning. Optical flashes, not ground strikes; unsearched time
> is unknown, not zero.

Also credit EUMETSAT as the data source: "Contains EUMETSAT MTG Lightning Imager L2-LFL data,
collection EO:EUM:DAT:0691". Follow the attribution wording that EUMETSAT's data policy
requires at the time of use. The exact wording has not been verified for this note. The code
is licensed under the repository `LICENSE`.
