#!/usr/bin/env python3
"""Extract located GB transmission substations from data-grid-gb, pinned by SHA-256.

Input: Ventusltd/data-grid-gb ``derived/connection-points.v3.json`` at the commit
and SHA-256 recorded in ``sources/gb-transmission-substations.source.json``. The
build refuses any other bytes. Output: ``sources/gb-transmission-substations.v1.json``,
a small extract (site code, name, owner, voltages, coordinates, how the
coordinates were matched) of the connection points that carry a location.

Attribution travels with the extract. Names, owners and voltages are NESO ETYS
2025 identities as derived by data-grid-gb; coordinates are OpenStreetMap
contributors' geometry joined by data-grid-gb (ODbL 1.0). Unlocated connection
points are counted, not invented: nothing here guesses a coordinate.

Usage:
  python tools/extract_substations.py --upstream /path/to/connection-points.v3.json
  python tools/extract_substations.py --fetch      # download the pinned raw URL
  python tools/extract_substations.py --check      # re-derive and compare, no write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "sources" / "gb-transmission-substations.source.json"
OUT = ROOT / "sources" / "gb-transmission-substations.v1.json"
SCHEMA = "uk-lightning.gb-transmission-substations.v1"


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def load_pin() -> dict:
    return json.loads(PIN.read_text(encoding="utf-8"))


def read_upstream(pin: dict, path: Path | None, fetch: bool) -> bytes:
    if path:
        blob = Path(path).read_bytes()
    elif fetch:
        with urllib.request.urlopen(pin["raw_url"], timeout=60) as response:  # noqa: S310 - pinned https URL
            blob = response.read()
    else:
        raise SystemExit("give --upstream PATH or --fetch")
    digest = sha256_bytes(blob)
    if digest != pin["sha256"]:
        raise SystemExit(f"upstream sha256 {digest} != pinned {pin['sha256']}; refusing to extract")
    if len(blob) != pin["bytes"]:
        raise SystemExit(f"upstream bytes {len(blob)} != pinned {pin['bytes']}; refusing to extract")
    return blob


def extract(blob: bytes, pin: dict) -> dict:
    product = json.loads(blob)
    if product.get("schema") != pin["upstream_schema"]:
        raise SystemExit("upstream schema mismatch")
    points = product["connection_points"]
    located = []
    for point in points:
        location = point.get("location")
        if not location:
            continue
        lon, lat = float(location["lon"]), float(location["lat"])
        located.append({
            "site_code": point["site_code"],
            "name": point["name"],
            "transmission_owner": point.get("transmission_owner"),
            "max_kv": max(point.get("voltages_kv") or [0]) or None,
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            "matched_by": location.get("matched_by"),
        })
    located.sort(key=lambda row: row["site_code"])
    codes = [row["site_code"] for row in located]
    if len(codes) != len(set(codes)):
        raise SystemExit("duplicate site_code among located points")
    return {
        "schema": SCHEMA,
        "what_this_is": ("GB transmission connection points (132 kV and above) that data-grid-gb could locate, "
                         "extracted for distance rollups only. A point is a joined map location, not a surveyed "
                         "site boundary."),
        "derived_from": {
            "repository": pin["repository"], "commit": pin["commit"], "path": pin["path"],
            "sha256": pin["sha256"], "bytes": pin["bytes"], "upstream_schema": pin["upstream_schema"],
        },
        "attribution": pin["attribution"],
        "counts": {"connection_points": len(points), "located": len(located),
                   "unlocated_not_included": len(points) - len(located)},
        "substations": located,
    }


def serialise(obj: dict) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream", type=Path)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--check", action="store_true", help="compare with the committed extract; do not write")
    args = parser.parse_args(argv)
    pin = load_pin()
    content = serialise(extract(read_upstream(pin, args.upstream, args.fetch), pin))
    if args.check:
        if OUT.read_text(encoding="utf-8") != content:
            print("extract differs from the pinned upstream", file=sys.stderr)
            return 1
        print("extract matches the pinned upstream")
        return 0
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(content.encode())} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
