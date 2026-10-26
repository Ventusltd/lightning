#!/usr/bin/env python3
"""UK lightning flash GeoJSON from EUMETSAT Meteosat LI L2-LFL.

Optical flashes observed from orbit (including intracloud), NOT proven
cloud-to-ground strikes. This produces public map data, not safety advice.
Only genuine downloaded detections are published; no sample points.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

COLLECTION = "EO:EUM:DAT:0691"  # LI Lightning Flashes - MTG 0 degree
PROVIDER = "EUMETSAT MTG Lightning Imager L2-LFL"
SOURCE_URL = "https://api.eumetsat.int/data/browse/collections/EO%3AEUM%3ADAT%3A0691?format=html"
# UK coverage AND neighbouring waters; this rectangle is NOT the UK border.
UK_BOUNDS = (-11.0, 49.5, 3.0, 61.5)  # west, south, east, north
MAX_RENDER_POINTS = 40000


def parse_utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def iso_utc(value) -> str:
    """Handle Python datetime, cftime and numpy-style date representations."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    text = value.strftime("%Y-%m-%dT%H:%M:%S.%f") if hasattr(value, "strftime") else str(value)
    return text.rstrip("0").rstrip(".") + "Z" if not text.endswith("Z") else text


def inside_uk_view(longitude: float, latitude: float) -> bool:
    w, s, e, n = UK_BOUNDS
    return math.isfinite(longitude) and math.isfinite(latitude) and w <= longitude <= e and s <= latitude <= n


def build_feature(lon, lat, timestamp, product_name: str, index: int):
    lon, lat = float(lon), float(lat)
    if not inside_uk_view(lon, lat):
        return None
    observed = iso_utc(timestamp)
    key = f"{product_name}:{index}"
    return {
        "type": "Feature",
        "id": key,
        "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
        "properties": {
            "name": "Lightning flash " + observed[:16].replace("T", " ") + " UTC",
            "raw_tech": "EUMETSAT optical flash (not a confirmed ground strike)",
            "status": "Satellite detection",
            "time_utc": observed,
            "source": "EUMETSAT",
            "source_product": "MTG LI L2-LFL",
            "observation_kind": "optical_lightning_flash",
        },
    }


def decode_body(content: bytes, product_name: str, start: datetime, end: datetime):
    """Read actual LFL BODY NetCDF with CF scale/offset and UTC flash times."""
    from netCDF4 import Dataset, num2date

    with Dataset("lightning-body", memory=content) as ds:
        vars_ = ds.variables
        for field in ("longitude", "latitude", "flash_time"):
            if field not in vars_:
                raise ValueError(f"{product_name}: missing expected LFL field {field}")
        lons = vars_["longitude"][:]
        lats = vars_["latitude"][:]
        times_var = vars_["flash_time"]
        times = times_var[:]
        if len(lons) != len(lats) or len(lons) != len(times):
            raise ValueError(f"{product_name}: coordinate/time dimensions do not match")
        units = getattr(times_var, "units", "seconds since 2000-01-01 00:00:00")
        calendar = getattr(times_var, "calendar", "standard")
        dates = num2date(times, units=units, calendar=calendar)
        results = []
        for i in range(len(lons)):
            try:
                if any(getattr(x, "mask", False) is True for x in (lons[i], lats[i], times[i])):
                    continue
                dt = parse_utc(iso_utc(dates[i]))
                if not start <= dt < end:
                    continue
                feat = build_feature(lons[i], lats[i], dt, product_name, i)
                if feat:
                    results.append(feat)
            except (ValueError, TypeError, OverflowError):
                continue
        return results


def decode_product(blob: bytes, product_name: str, start: datetime, end: datetime):
    """EUMETSAT packages products as ZIP archives with *BODY*.nc."""
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        raise ValueError(f"{product_name}: expected EUMETSAT product ZIP")
    found = False
    features = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            if name.upper().endswith(".NC") and "BODY" in Path(name).name.upper():
                found = True
                features.extend(decode_body(archive.read(name), product_name, start, end))
    if not found:
        raise ValueError(f"{product_name}: ZIP contained no BODY NetCDF")
    return features


def download(start: datetime, end: datetime):
    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("Missing EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET. Never commit credentials.")
    import eumdac

    datastore = eumdac.DataStore(eumdac.AccessToken((key, secret)))
    products = datastore.get_collection(COLLECTION).search(
        dtstart=start.replace(tzinfo=None), dtend=end.replace(tzinfo=None)
    )
    count = getattr(products, "total_results", None)
    if count == 0:
        raise RuntimeError("No EUMETSAT product files returned. Existing map data left unchanged.")
    found = []
    downloaded = 0
    for product in products:
        downloaded += 1
        if downloaded > 1000:
            raise RuntimeError("Too many products in one run; reduce --hours.")
        print(f"Product {downloaded}: {product}", file=sys.stderr)
        with product.open() as stream:
            found.extend(decode_product(stream.read(), str(product), start, end))
    if downloaded == 0:
        raise RuntimeError("Empty product search. Existing map data left unchanged.")
    return found, downloaded


def read_previous(path: Path):
    if not path.exists():
        return []
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("type") != "FeatureCollection":
        raise ValueError("Previous GeoJSON is not a FeatureCollection")
    return obj.get("features", [])


def assemble(previous, incoming, now: datetime, retention_days: int = 7):
    cutoff = now - timedelta(days=retention_days)
    by_id = {}
    for feature in list(previous) + list(incoming):
        if feature.get("type") != "Feature" or feature.get("geometry", {}).get("type") != "Point":
            continue
        props = feature.get("properties", {})
        if props.get("source_product") != "MTG LI L2-LFL":
            continue
        try:
            timestamp = parse_utc(props["time_utc"])
            x, y = feature["geometry"]["coordinates"]
            if cutoff <= timestamp <= now and inside_uk_view(float(x), float(y)):
                by_id[str(feature["id"])] = feature
        except (KeyError, ValueError, TypeError):
            continue
    rows = sorted(by_id.values(), key=lambda f: (f["properties"]["time_utc"], str(f["id"])))
    total = len(rows)
    if total > MAX_RENDER_POINTS:
        rows = rows[-MAX_RENDER_POINTS:]
    return {
        "type": "FeatureCollection",
        "metadata": {
            "source": PROVIDER, "source_url": SOURCE_URL,
            "license": "EUMETSAT data policy: NoConditions; attribution retained",
            "coverage": "UK geographic bounding box and neighbouring waters, not national boundary",
            "bounds_wsen": list(UK_BOUNDS),
            "observation": "satellite optical flashes; not verified ground strike locations",
            "retention_days": retention_days,
            "total_before_display_cap": total,
            "display_cap": MAX_RENDER_POINTS,
            "latest_detection_utc": rows[-1]["properties"]["time_utc"] if rows else None,
            "status": "ready",
        },
        "features": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=3, help="How far back to fetch from EUMETSAT; 1-48")
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path("data/uk_lightning.geojson"))
    parser.add_argument("--history-dir", type=Path, default=None,
                        help="Optional append-only one-calendar-year UTC daily archive")
    args = parser.parse_args()
    if not 1 <= args.hours <= 48 or not 1 <= args.retention_days <= 31:
        parser.error("hours must be 1..48 and retention-days must be 1..31")
    now = datetime.now(timezone.utc)
    beginning = now - timedelta(hours=args.hours)
    incoming, product_count = download(beginning, now)
    existing = read_previous(args.out)
    result = assemble(existing, incoming, now, retention_days=args.retention_days)
    if args.history_dir:
        # The archive receives uncapped genuine incoming detections, plus the
        # previously published rolling feed as a clearly partial initial seed.
        # The normal seven-day feed remains byte-compatible for existing users.
        from history import archive_observations, successful_scan
        scan = successful_scan(iso_utc(beginning), iso_utc(now), product_count)
        archive = archive_observations(
            args.history_dir, [*existing, *incoming], now=now, scanned_window=scan
        )
        print(f"Archived {archive['index']['archived_observations']} dated optical "
              f"observations across {archive['index']['archived_days_with_flashes']} "
              "UTC days (availability is not continuous).")
    # Refresh provenance only when a successful query returned product files.
    result["metadata"]["products_examined"] = product_count
    result["metadata"]["published_at_utc"] = iso_utc(now)
    result["metadata"]["first_detection_utc"] = (
        result["features"][0]["properties"]["time_utc"] if result["features"] else None
    )
    result["metadata"]["last_detection_utc"] = result["metadata"]["latest_detection_utc"]
    result["metadata"]["query_start_utc"] = iso_utc(beginning)
    result["metadata"]["query_end_utc"] = iso_utc(now)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Published {len(result['features'])} genuine optical flashes from {product_count} EUMETSAT products.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
