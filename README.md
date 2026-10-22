# UK lightning — public optical flash map

Open-source UK-wide lightning visualisation for [GridAtlas](https://ventusltd.github.io/gridatlas/atlas/). **No particular site is targeted, analysed or named.** This is public weather information, not a risk assessment.

## Source and meaning

Official [EUMETSAT Meteosat Third Generation Lightning Imager (LI)](https://api.eumetsat.int/data/browse/collections/EO%3AEUM%3ADAT%3A0691?format=html) Level 2 Lightning Flashes (LFL), collection `EO:EUM:DAT:0691`. This collection is listed with `NoConditions` data policy; attribution remains in the GeoJSON metadata.

The satellite detects **optical lightning flashes**, including inside clouds. Map dots are **not verified ground-impact locations**. UK geographic view bounds are longitude −11° to +3° and latitude 49.5° to 61.5°, including adjacent waters; this is a rectangle, not a national-border polygon.

## Data files

- `collect.py`: downloads genuine LFL products with `eumdac`, reads NetCDF BODY data, filters UK coordinates and exports GeoJSON.
- `data/uk_lightning.geojson`: data feed; initially **zero observations** pending ingestion. Zero in the placeholder does not mean no lightning.
- `.github/workflows/update-lightning.yml`: three-hourly update and manually selectable 1–48 hour backfill; seven-day retained rolling window.
- `tests/test_collect.py`: offline location, timestamp and deduplication tests.

## Enable collection

1. Register for a free API key at [EUMETSAT Data Store](https://data.eumetsat.int/).
2. Add repository Actions secrets `EUMETSAT_CONSUMER_KEY` and `EUMETSAT_CONSUMER_SECRET`.
3. Select **Actions → UK lightning optical flashes → Run workflow** (start with `hours=3`). Thereafter the scheduled update runs every three hours.
4. View the GeoJSON at [data/uk_lightning.geojson](data/uk_lightning.geojson). GridAtlas uses the raw-file endpoint: `https://raw.githubusercontent.com/Ventusltd/lightning/main/data/uk_lightning.geojson`.

Local Python 3.12 execution:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python collect.py --hours 3
```

The program requires the two EUMETSAT environment variables. No API keys should ever be committed. Download failures preserve the existing feed. Automatic rolling output is capped at 40,000 recent flash points for public mobile maps. The observations are not evidence that a particular building or installation was struck.
