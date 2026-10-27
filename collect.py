#!/usr/bin/env python3
"""UK lightning flash GeoJSON from EUMETSAT Meteosat LI L2-LFL.

Optical flashes observed from orbit (including intracloud), NOT proven
cloud-to-ground strikes. This produces public map data, not safety advice.
Only genuine downloaded detections are published; no sample points.

Collection model (data plane only, nothing here touches GridAtlas):
- hourly runs; each run searches a self-healing lookback window
  ``max(6 h, now - last successful scan end + 30 min)`` capped at 48 h, so a
  late or failed run is caught up by the next one instead of leaving a gap;
- every examined EUMETSAT product is a 10-minute sensing slot; the run records
  the slots it actually examined, and only those slots count as "searched".
  The slot containing ``now`` is normally not yet published, so the tail of
  the query window is unobserved and is recorded as such (unknown, not zero);
- one unreadable product (ZIP without a BODY NetCDF, corrupt ZIP or NetCDF)
  does not stop the run: its slot is UNPARSED, i.e. not searched time, so it
  stays unknown (never zero) and is listed with its reason in the run record.
  Network/auth/rate-limit errors still stop the run, and so does a UTC date
  with more than ``MAX_UNPARSED_DAY_FRACTION`` of its slots unparsed;
- the live 7-day feed is written first; the one-year archive and the status
  file are produced by separate steps from the run record this module leaves
  behind, so an archive failure can never block the feed.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

COLLECTION = "EO:EUM:DAT:0691"  # LI Lightning Flashes - MTG 0 degree
PROVIDER = "EUMETSAT MTG Lightning Imager L2-LFL"
SOURCE_URL = "https://api.eumetsat.int/data/browse/collections/EO%3AEUM%3ADAT%3A0691?format=html"
# UK coverage AND neighbouring waters; this rectangle is NOT the UK border.
UK_BOUNDS = (-11.0, 49.5, 3.0, 61.5)  # west, south, east, north
MAX_RENDER_POINTS = 40000

# Self-healing lookback window (hours / minutes); see lookback_window().
LOOKBACK_MIN_HOURS = 6
LOOKBACK_MAX_HOURS = 48
LOOKBACK_MARGIN_MINUTES = 30
# LI L2-LFL "ARC" products cover 10-minute sensing slots (144 per UTC day).
SLOT_SECONDS = 600
# Products appear a few minutes after the slot ends; a gap ending within this
# many seconds of ``now`` is "not yet published", not a source outage.
PUBLICATION_GRACE_SECONDS = 15 * 60
RUN_RECORD_SCHEMA = "uk-lightning-run-record.v1"
# ..._L2PF_OPE_<sensing start YYYYMMDDHHMMSS>_<sensing end YYYYMMDDHHMMSS>_N__O_<slot>_0000
PRODUCT_SLOT = re.compile(r"_OPE_(\d{14})_(\d{14})_")
# A UTC date with more unparsed slots than this fraction of its slots in the
# query window is not recorded at all: the run stops and the date stays unknown.
MAX_UNPARSED_DAY_FRACTION = 0.20
# Per-product CONTENT errors raised while decoding bytes already downloaded
# (ZIP without BODY, corrupt ZIP, corrupt/odd NetCDF: netCDF4 raises OSError or
# RuntimeError). Downloading happens outside the decode, so network, auth and
# rate-limit errors are never caught here; nor is a missing netCDF4 (ImportError).
PRODUCT_CONTENT_ERRORS = (ValueError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError,
                          OSError, RuntimeError, KeyError, IndexError, TypeError)


class TooManyUnparsed(RuntimeError):
    """A date has too many unreadable products (or none readable): stop, leave it unknown."""

    def __init__(self, message: str, unparsed=()):
        super().__init__(message)
        self.unparsed = list(unparsed)


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
    if text.endswith("Z"):
        return text
    # Only fractional seconds may lose trailing zeros; "12:32:20" must stay "12:32:20".
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text + "Z"


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


def decode_product(blob: bytes, product_name: str, start: datetime, end: datetime, *, body_decoder=None):
    """EUMETSAT packages products as ZIP archives with *BODY*.nc."""
    body_decoder = body_decoder or decode_body
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        raise ValueError(f"{product_name}: expected EUMETSAT product ZIP")
    found = False
    features = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            if name.upper().endswith(".NC") and "BODY" in Path(name).name.upper():
                found = True
                features.extend(body_decoder(archive.read(name), product_name, start, end))
    if not found:
        raise ValueError(f"{product_name}: ZIP contained no BODY NetCDF")
    return features


def search_products(start: datetime, end: datetime):
    """The real EUMETSAT product search (network); products are opened lazily."""
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
    return products


def unparsed_entry(product_name: str, error: BaseException) -> dict:
    slot = product_slot(product_name)
    return {
        "product": product_name,
        "reason": f"{type(error).__name__}: {error}"[:1000],
        "slot_start_utc": iso_utc(slot[0]) if slot else None,
        "slot_end_utc": iso_utc(slot[1]) if slot else None,
    }


def download_products(products, start: datetime, end: datetime, *, body_decoder=None):
    """Download and decode each product: (features, readable count, readable names, unparsed).

    The download (``open``/``read``) is outside the per-product guard, so any
    network, auth, rate-limit or timeout error propagates and stops the run.
    Only a content error while decoding bytes already in hand makes that one
    product UNPARSED; its name is then NOT among the examined names, so its slot
    is not searched time (unknown, never zero).
    """
    found, names, unparsed = [], [], []
    downloaded = 0
    for product in products:
        downloaded += 1
        if downloaded > 1000:
            raise RuntimeError("Too many products in one run; reduce --hours.")
        name = str(product)
        print(f"Product {downloaded}: {name}", file=sys.stderr)
        with product.open() as stream:
            blob = stream.read()
        try:
            features = decode_product(blob, name, start, end, body_decoder=body_decoder)
        except PRODUCT_CONTENT_ERRORS as error:
            entry = unparsed_entry(name, error)
            unparsed.append(entry)
            print(f"UNPARSED product {name}: {entry['reason']} (slot left unknown)", file=sys.stderr)
            continue
        names.append(name)
        found.extend(features)
    if downloaded == 0:
        raise RuntimeError("Empty product search. Existing map data left unchanged.")
    return found, len(names), names, unparsed


def download(start: datetime, end: datetime):
    """Return (features, readable products, readable names, unparsed products) for a real search."""
    return download_products(search_products(start, end), start, end)


def unpack_download(result):
    """Downloaders return (features, count, names) or (features, count, names, unparsed)."""
    if len(result) == 3:
        return (*result, [])
    features, count, names, unparsed = result
    return features, count, names, list(unparsed or [])


def check_unparsed(unparsed, names, beginning: datetime, end: datetime, *,
                   max_fraction: float = MAX_UNPARSED_DAY_FRACTION) -> dict:
    """Stop (TooManyUnparsed) when no product was readable or a UTC date has too many unparsed slots.

    An unparsed slot already covered by a readable product (e.g. a short PART
    product inside a regular slot) is not unknown time and is not counted. The
    denominator is the number of 10-minute slots of that date inside the query
    window (144 for a whole-day rebuild). Returns {date: unparsed slot count}.
    """
    unparsed = list(unparsed)
    if not unparsed:
        return {}
    if not names:
        raise TooManyUnparsed(f"all {len(unparsed)} product(s) unreadable; nothing was searched "
                              f"(first: {unparsed[0]['reason']})", unparsed)
    readable = merge_ranges([slot for slot in (product_slot(n) for n in names) if slot])
    per_day: dict[str, set] = {}
    day = beginning.date()
    while datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) < end:
        per_day[day.isoformat()] = set()
        day += timedelta(days=1)
    for entry in unparsed:
        slot = product_slot(entry["product"])
        if slot is None:  # unknown slot: conservatively counts against every date of the window
            for marks in per_day.values():
                marks.add(entry["product"])
            continue
        s, e = max(slot[0], beginning), min(slot[1], end)
        if e <= s or any(rs <= s and e <= re_ for rs, re_ in readable):
            continue
        per_day.setdefault(s.date().isoformat(), set()).add(s)
    counts = {}
    for day_text, marks in per_day.items():
        if not marks:
            continue
        day_start = datetime.combine(datetime.fromisoformat(day_text).date(), datetime.min.time(), tzinfo=timezone.utc)
        span = (min(end, day_start + timedelta(days=1)) - max(beginning, day_start)).total_seconds()
        slots = max(1, math.ceil(span / SLOT_SECONDS))
        counts[day_text] = len(marks)
        if len(marks) > max_fraction * slots:
            raise TooManyUnparsed(
                f"{day_text}: {len(marks)} of {slots} 10-minute slots unparsed (more than "
                f"{max_fraction:.0%}); date not recorded, left unknown (first: {unparsed[0]['reason']})", unparsed)
    return counts


def unparsed_slot_starts(unparsed, names, beginning: datetime, end: datetime) -> list[str]:
    """Slot starts of unparsed products NOT covered by a readable product (unknown time)."""
    readable = merge_ranges([slot for slot in (product_slot(n) for n in names) if slot])
    starts = set()
    for entry in unparsed:
        slot = product_slot(entry["product"])
        if slot is None:
            continue
        s, e = max(slot[0], beginning), min(slot[1], end)
        if e > s and not any(rs <= s and e <= re_ for rs, re_ in readable):
            starts.add(s)
    return [iso_utc(x) for x in sorted(starts)]


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


# --- self-healing lookback -------------------------------------------------

def lookback_window(
    now: datetime,
    last_success_end: datetime | None,
    *,
    minimum_hours: float = LOOKBACK_MIN_HOURS,
    maximum_hours: float = LOOKBACK_MAX_HOURS,
    margin_minutes: float = LOOKBACK_MARGIN_MINUTES,
) -> tuple[timedelta, bool]:
    """``max(minimum, now - last_success_end + margin)`` capped at ``maximum``.

    Returns (window, capped). ``capped`` is True when the gap since the last
    successful scan exceeds the maximum: the part of the gap older than
    ``maximum`` can never be searched by the scheduled job and stays UNKNOWN.
    """
    minimum = timedelta(hours=minimum_hours)
    maximum = timedelta(hours=maximum_hours)
    wanted = minimum
    if last_success_end is not None:
        wanted = max(minimum, now - last_success_end + timedelta(minutes=margin_minutes))
    return min(wanted, maximum), wanted > maximum


def read_last_success(status_path: Path | None, history_dir: Path | None) -> tuple[datetime | None, str]:
    """Last successful scan end from data/status.json, else the archive index."""
    if status_path and status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            value = status.get("last_successful_scan_end_utc")
            if value:
                return parse_utc(value), "status"
        except (ValueError, TypeError, AttributeError):
            pass
    index_path = history_dir / "index.json" if history_dir else None
    if index_path and index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            value = index.get("last_successful_scan_end_utc")
            if value:
                return parse_utc(value), "index"
            ends = [parse_utc(w["end_utc"]) for w in index.get("scan_windows", []) if w.get("end_utc")]
            if ends:
                return max(ends), "index"
        except (ValueError, TypeError, AttributeError, KeyError):
            pass
    return None, "default"


# --- examined 10-minute product slots --------------------------------------

def product_slot(product_name: str) -> tuple[datetime, datetime] | None:
    """Sensing slot [start, end) encoded in an LFL product name, or None."""
    match = PRODUCT_SLOT.search(product_name or "")
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        end = datetime.strptime(match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if end <= start or (end - start) > timedelta(hours=1):
        return None
    return start, end


def merge_ranges(ranges, *, tolerance_seconds: float = 1.0) -> list[tuple[datetime, datetime]]:
    """Union of half-open ranges; adjacent slots (within tolerance) join."""
    ordered = sorted((s, e) for s, e in ranges if e > s)
    merged: list[list[datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + timedelta(seconds=tolerance_seconds):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def scanned_ranges(product_names, beginning: datetime, now: datetime) -> tuple[list[tuple[datetime, datetime]], list[datetime], bool]:
    """Searched coverage of a run = union of examined slots clipped to [beginning, now).

    Returns (ranges, slot_starts, parsed). When no product name carries a
    slot, the whole query window is returned and ``parsed`` is False so the
    record can say the coverage is the requested window, not examined slots.
    """
    slots = []
    for name in product_names:
        slot = product_slot(name)
        if slot:
            slots.append(slot)
    if not slots:
        return ([(beginning, now)] if now > beginning else []), [], False
    clipped = []
    for start, end in slots:
        s, e = max(start, beginning), min(end, now)
        if e > s:
            clipped.append((s, e))
    return merge_ranges(clipped), sorted({s for s, _ in slots}), True


def unscanned_ranges(ranges, beginning: datetime, now: datetime, *, grace_seconds: int = PUBLICATION_GRACE_SECONDS):
    """Gaps inside [beginning, now) no examined slot covers.

    Returns (gaps, tail_seconds): ``tail_seconds`` is the unobserved tail
    ending at ``now`` (products not yet published); ``gaps`` are the other
    holes, i.e. genuine source gaps (EUMETSAT slots missing at search time).
    """
    gaps = []
    cursor = beginning
    for start, end in sorted(ranges):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    tail_seconds = 0
    if cursor < now:
        tail_seconds = int((now - cursor).total_seconds())
        if tail_seconds > grace_seconds:
            gaps.append((cursor, now))
    return gaps, tail_seconds


def _iso_pairs(ranges):
    return [{"start_utc": iso_utc(s), "end_utc": iso_utc(e)} for s, e in ranges]


def build_run_record(
    *,
    now: datetime,
    beginning: datetime,
    lookback_source: str,
    lookback_capped: bool,
    last_success_before: datetime | None,
    product_names,
    products_examined: int,
    incoming,
    existing,
    feed,
    unparsed=(),
) -> dict:
    """Everything the archive and status steps need; never committed itself."""
    ranges, slot_starts, parsed = scanned_ranges(product_names, beginning, now)
    gaps, tail = unscanned_ranges(ranges, beginning, now)
    return {
        "schema": RUN_RECORD_SCHEMA,
        "run_utc": iso_utc(now),
        "outcome": "success",
        "error": None,
        "query_start_utc": iso_utc(beginning),
        "query_end_utc": iso_utc(now),
        "lookback_hours": round((now - beginning).total_seconds() / 3600, 4),
        "lookback_source": lookback_source,
        "lookback_capped": lookback_capped,
        "last_successful_scan_end_utc_before_run": iso_utc(last_success_before) if last_success_before else None,
        "products_examined": products_examined,
        "slot_seconds": SLOT_SECONDS,
        "slots_examined": len(slot_starts),
        "slots_examined_utc": [iso_utc(s) for s in slot_starts],
        "slots_parsed": parsed,
        "products_unparsed": len(unparsed),
        "unparsed_products": list(unparsed),
        "unparsed_slots_utc": unparsed_slot_starts(unparsed, product_names, beginning, now),
        "max_unparsed_day_fraction": MAX_UNPARSED_DAY_FRACTION,
        "scanned_ranges": _iso_pairs(ranges),
        "scanned_end_utc": iso_utc(max(e for _, e in ranges)) if ranges else None,
        "unscanned_ranges": _iso_pairs(gaps),
        "tail_unobserved_seconds": tail,
        "incoming_features": len(incoming),
        "feed_features": len(feed["features"]),
        "feed_total_before_display_cap": feed["metadata"]["total_before_display_cap"],
        "feed_latest_detection_utc": feed["metadata"]["latest_detection_utc"],
        "note": ("Only examined 10-minute slots count as searched; the unobserved tail, gaps and unparsed "
                 "(unreadable) product slots are unknown, not zero."),
        # Uncapped incoming detections plus the previously published feed (the
        # seed on the first archive run; idempotent afterwards).
        "features": [*existing, *incoming],
    }


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")


def rebuild_day_record(day: str, run_record: Path, *, now: datetime | None = None, downloader=None) -> int:
    """Fresh EUMETSAT search of one whole UTC date, for ``history.py recover --rebuild``.

    Writes ONLY the run record (never the public feed, status or archive). Its
    ``run_utc`` is the real time of this search, so recover can check it was made
    after the loss was detected; its examined slots and flashes are then checked
    against the archive's recorded scan windows and count before anything changes.
    """
    from datetime import date as _date
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = datetime.combine(_date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc)
    end = min(start + timedelta(days=1), now)
    if end <= start:
        raise ValueError(f"{day} has not started yet")
    incoming, product_count, names, unparsed = unpack_download((downloader or download)(start, end))
    try:
        check_unparsed(unparsed, names, start, end)
    except TooManyUnparsed as error:
        write_json(run_record, {
            "schema": RUN_RECORD_SCHEMA, "run_utc": iso_utc(now), "outcome": "failure",
            "error": f"{type(error).__name__}: {error}", "rebuild_day_utc": day,
            "query_start_utc": iso_utc(start), "query_end_utc": iso_utc(end),
            "products_examined": product_count, "products_unparsed": len(unparsed),
            "unparsed_products": unparsed, "scanned_ranges": [], "features": [],
        })
        raise
    record = build_run_record(
        now=end, beginning=start, lookback_source="rebuild", lookback_capped=False, last_success_before=None,
        product_names=names, products_examined=product_count, incoming=incoming, existing=[],
        feed={"features": [], "metadata": {"total_before_display_cap": None, "latest_detection_utc": None}},
        unparsed=unparsed,
    )
    record["run_utc"] = iso_utc(now)
    record["rebuild_day_utc"] = day
    record["note"] = ("Rebuild search of one UTC date for history.py recover --rebuild; not a feed run. "
                      "Only examined 10-minute slots count as searched.")
    write_json(run_record, record)
    print(f"Rebuild of {day}: {len(incoming)} flashes from {product_count} products "
          f"({record['slots_examined']} slots examined, {len(record['unscanned_ranges'])} gaps, "
          f"{record['products_unparsed']} unparsed product(s)).", file=sys.stderr)
    return 0


def main(argv=None, *, now: datetime | None = None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--hours", type=float, default=None, help="Explicit lookback from EUMETSAT; 1-48")
    window.add_argument("--auto-window", action="store_true",
                        help=f"max({LOOKBACK_MIN_HOURS} h, now - last successful scan end + "
                             f"{LOOKBACK_MARGIN_MINUTES} min) capped at {LOOKBACK_MAX_HOURS} h")
    window.add_argument("--rebuild-day", default=None, metavar="YYYY-MM-DD",
                        help="Search one whole past UTC date and write only --run-record (for history.py recover --rebuild)")
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path("data/uk_lightning.geojson"))
    parser.add_argument("--status", type=Path, default=Path("data/status.json"),
                        help="Collector status file read for the last successful scan end")
    parser.add_argument("--history-dir", type=Path, default=Path("data/history"),
                        help="Archive directory; its index is the fallback source of the last scan end")
    parser.add_argument("--run-record", type=Path, default=None,
                        help="Where to leave the uncapped run record for the archive/status steps (not committed)")
    args = parser.parse_args(argv)
    if args.rebuild_day:
        if not args.run_record:
            parser.error("--rebuild-day needs --run-record")
        return rebuild_day_record(args.rebuild_day, args.run_record)
    if args.hours is not None and not 1 <= args.hours <= LOOKBACK_MAX_HOURS:
        parser.error(f"hours must be 1..{LOOKBACK_MAX_HOURS}")
    if not 1 <= args.retention_days <= 31:
        parser.error("retention-days must be 1..31")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    last_success, source = read_last_success(args.status, args.history_dir)
    capped = False
    if args.hours is not None:
        window, source = timedelta(hours=args.hours), "explicit"
    elif args.auto_window:
        window, capped = lookback_window(now, last_success)
    else:
        window, source = timedelta(hours=LOOKBACK_MIN_HOURS), "default"
    beginning = now - window
    print(f"Query window {iso_utc(beginning)} -> {iso_utc(now)} ({window.total_seconds() / 3600:.2f} h, "
          f"source={source}, capped={capped}, last successful scan end={iso_utc(last_success) if last_success else 'none'})",
          file=sys.stderr)
    unparsed = []
    try:
        incoming, product_count, names, unparsed = unpack_download(download(beginning, now))
        check_unparsed(unparsed, names, beginning, now)
        existing = read_previous(args.out)
        result = assemble(existing, incoming, now, retention_days=args.retention_days)
        record = build_run_record(
            now=now, beginning=beginning, lookback_source=source, lookback_capped=capped,
            last_success_before=last_success, product_names=names, products_examined=product_count,
            incoming=incoming, existing=existing, feed=result, unparsed=unparsed,
        )
    except Exception as error:  # noqa: BLE001 - the record must name any failure
        if args.run_record:
            write_json(args.run_record, {
                "schema": RUN_RECORD_SCHEMA, "run_utc": iso_utc(now), "outcome": "failure",
                "error": f"{type(error).__name__}: {error}", "query_start_utc": iso_utc(beginning),
                "query_end_utc": iso_utc(now), "lookback_hours": round(window.total_seconds() / 3600, 4),
                "lookback_source": source, "lookback_capped": capped,
                "last_successful_scan_end_utc_before_run": iso_utc(last_success) if last_success else None,
                "products_examined": 0, "products_unparsed": len(unparsed), "unparsed_products": unparsed,
                "scanned_ranges": [], "features": [],
            })
        raise
    # Refresh provenance only when a successful query returned product files.
    meta = result["metadata"]
    meta["products_examined"] = product_count
    meta["slots_examined"] = record["slots_examined"]
    meta["products_unparsed"] = record["products_unparsed"]
    meta["query_start_utc"] = iso_utc(beginning)
    meta["query_end_utc"] = iso_utc(now)
    meta["scan_end_utc"] = record["scanned_end_utc"]
    meta["published_at_utc"] = iso_utc(now)
    meta["first_detection_utc"] = result["features"][0]["properties"]["time_utc"] if result["features"] else None
    meta["last_detection_utc"] = meta["latest_detection_utc"]
    meta["coverage_note"] = ("query_start/end are the last search window only; earlier hours of the "
                             "7-day retention were searched by earlier runs or not at all (see data/history/index.json)")
    write_json(args.out, result)
    if args.run_record:
        write_json(args.run_record, record)
    print(f"Published {len(result['features'])} genuine optical flashes from {product_count} EUMETSAT products "
          f"({record['slots_examined']} slots examined, {len(record['unscanned_ranges'])} gaps, "
          f"tail unobserved {record['tail_unobserved_seconds']} s, "
          f"{record['products_unparsed']} unparsed product(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
