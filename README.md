# UK lightning — public optical flash map

Open-source UK-wide lightning visualisation for [GridAtlas](https://ventusltd.github.io/gridatlas/atlas/). **No particular site is targeted, analysed or named.** This is public weather information, not a risk assessment.

## Source and meaning

Official [EUMETSAT Meteosat Third Generation Lightning Imager (LI)](https://api.eumetsat.int/data/browse/collections/EO%3AEUM%3ADAT%3A0691?format=html) Level 2 Lightning Flashes (LFL), collection `EO:EUM:DAT:0691`. This collection is listed with `NoConditions` data policy; attribution remains in the GeoJSON metadata.

The satellite detects **optical lightning flashes**, including inside clouds. Map dots are **not verified ground-impact locations**. UK geographic view bounds are longitude −11° to +3° and latitude 49.5° to 61.5°, including adjacent waters; this is a rectangle, not a national-border polygon.

## Data files

- `collect.py`: downloads genuine LFL products with `eumdac`, reads NetCDF BODY data, filters UK coordinates and exports GeoJSON. Each run searches a self-healing lookback window `max(6 h, now − last successful scan end + 30 min)` capped at 48 h and records the examined 10-minute product slots.
- `data/uk_lightning.geojson`: live data feed, seven-day retention. Its `query_start/end_utc` describe only the last search window; earlier hours may be unsearched (see the archive index). Zero features never means no lightning.
- `history.py` and `data/history/`: one-year UTC archive — `index.json` (`uk-lightning-history-index.v2`, one row per UTC date with coverage none/partial/full, count null when never searched) and day shards `days/YYYY/MM/DD.json` (hourly parts above 8 MiB). See `docs/HISTORY_ONE_YEAR.md`.
- `status.py` and `data/status.json`: written after every run, successful or not (last error, consecutive failures, last successful scan end).
- `history-window.mjs`: DOM-free UTC date-window client (periods, navigation, budgeted tiered loading, coverage, UI states) for a GridAtlas implant to consume.
- `parquet_store.py`, `rollups.py`, `sql/`, `verify_parquet.py` and `data/parquet/`: a Parquet mirror of the archive (flashes partitioned `year=YYYY/month=MM`, ZSTD), per-day and per-10-minute-slot coverage (never searched is NULL, never 0), DuckDB rollups (daily, hourly, storm episodes, a <= 4 kB `headline.json`) and a SHA-256 manifest, verified fail-closed. The substation metrics are **optical-flash proximity**: satellite optical flashes within 5/10/20 km of a GB transmission connection point's map location (data-grid-gb, pinned by SHA-256; coordinates (c) OpenStreetMap contributors, ODbL). They are **not ground strikes, not confirmed strikes and not electrical faults**. See `docs/PARQUET-DUCKDB.md`.
- `.github/workflows/update-lightning.yml`: hourly update; the feed is committed first and separately from the archive, so an archive failure never blocks the feed; manual runs may set an explicit 1–48 hour lookback.
- `tests/`: offline Python (`unittest`) and Node (`node --test`) contracts.

## Enable collection

1. Register for a free API key at [EUMETSAT Data Store](https://data.eumetsat.int/).
2. Add repository Actions secrets `EUMETSAT_CONSUMER_KEY` and `EUMETSAT_CONSUMER_SECRET`.
3. Select **Actions → UK lightning optical flashes → Run workflow** (leave `hours` empty for the automatic window). Thereafter the scheduled update runs hourly.
4. View the GeoJSON at [data/uk_lightning.geojson](data/uk_lightning.geojson). GridAtlas uses the raw-file endpoint: `https://raw.githubusercontent.com/Ventusltd/lightning/main/data/uk_lightning.geojson`.

Local Python 3.12 execution:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
node --test tests/test_history_window.mjs
python collect.py --auto-window --run-record /tmp/run.json
python history.py ingest --archive-dir data/history --run-record /tmp/run.json
python status.py write --status data/status.json --run-record /tmp/run.json --collect-outcome success --archive-outcome success
python parquet_store.py build --archive-dir data/history --out data/parquet --if-archive
python verify_parquet.py --archive-dir data/history --out data/parquet --if-archive
```

The program requires the two EUMETSAT environment variables. No API keys should ever be committed. Download failures preserve the existing feed and archive and are recorded in `data/status.json`; unsearched time is unknown, not zero. Automatic rolling output is capped at 40,000 recent flash points for public mobile maps. The observations are not evidence that a particular building or installation was struck.

## Licence

Code: Apache License 2.0, the same licence as [GridAtlas](https://github.com/Ventusltd/gridatlas); see `LICENSE` and `NOTICE`. Data keeps its own terms: EUMETSAT LI L2-LFL (`NoConditions`, attribute EUMETSAT); OpenStreetMap-derived substation coordinates and proximity rollups (© OpenStreetMap contributors, ODbL 1.0, share-alike); NESO ETYS-derived names via data-grid-gb (terms to be confirmed).
