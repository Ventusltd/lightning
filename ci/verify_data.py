#!/usr/bin/env python3
"""Verify the committed data before it is pushed (quick) or after it landed (deep).

    python ci/verify_data.py [--data-dir data] [--staged] [--deep]

Checks, each only when its file exists:

- feed    ``uk_lightning.geojson``: JSON FeatureCollection, a features list of
          Point features, at most ``collect.MAX_RENDER_POINTS``, metadata object;
- status  ``status.json``: a JSON object of schema ``uk-lightning-status.*``;
- archive ``history/index.json``: ``history.verify_archive`` (index rows vs shard
          bytes, sha256 and counts; integrity block) - always blocking;
- grid    ``history/grid/``: ``grid.verify_grids``. Quick mode WARNS only,
          because the archive design lets a grid failure never block the
          archive (history.py ingest); ``--deep`` recounts every day from its
          shards and FAILS on any difference.

``--staged`` limits the checks to the parts with staged changes (``git diff
--cached``), which is how ``ci/push-with-replay.sh`` runs it before each
commit (PUSH_VERIFY): a feed-only commit never waits on the archive, and a
heartbeat-only commit is never blocked by an unrelated archive problem.

Exit 0 when every blocking check passes, 1 otherwise (``::error::`` lines).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grid  # noqa: E402
import history  # noqa: E402

MAX_RENDER_POINTS = 40000  # collect.MAX_RENDER_POINTS (collect.py is not imported: no eumdac needed)


def check_feed(path: Path) -> str:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise ValueError(f"{path}: not a GeoJSON FeatureCollection")
    features = obj.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{path}: features is not a list")
    if len(features) > MAX_RENDER_POINTS:
        raise ValueError(f"{path}: {len(features)} features exceed the {MAX_RENDER_POINTS} display cap")
    if not isinstance(obj.get("metadata"), dict):
        raise ValueError(f"{path}: metadata object missing")
    for i, feature in enumerate(features):
        geometry = (feature or {}).get("geometry") or {}
        coords = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coords, list) or len(coords) != 2:
            raise ValueError(f"{path}: feature {i} is not a Point")
    return f"feed ok ({len(features)} features)"


def check_status(path: Path) -> str:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or not str(obj.get("schema", "")).startswith("uk-lightning-status."):
        raise ValueError(f"{path}: not a uk-lightning-status object")
    return f"status ok ({obj['schema']})"


def staged_parts(data_dir: Path) -> set[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--", str(data_dir)],
                         check=True, capture_output=True, text=True).stdout.split()
    prefix = data_dir.as_posix().rstrip("/") + "/"
    parts = set()
    for name in out:
        rel = name[len(prefix):] if name.startswith(prefix) else name
        if rel == "uk_lightning.geojson":
            parts.add("feed")
        elif rel == "status.json":
            parts.add("status")
        elif rel.startswith("history/"):
            parts.add("archive")
    return parts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--staged", action="store_true", help="Only the parts with staged git changes")
    parser.add_argument("--deep", action="store_true", help="Recount grids from shards; a grid difference fails")
    args = parser.parse_args(argv)
    data = args.data_dir
    parts = staged_parts(data) if args.staged else {"feed", "status", "archive"}
    archive_dir = data / "history"
    failed = False

    def run(label, check, *, blocking=True):
        nonlocal failed
        try:
            print(f"verify-data: {check()}")
        except Exception as error:  # noqa: BLE001 - reported, then exit 1
            if blocking:
                failed = True
                print(f"::error::verify-data {label}: {type(error).__name__}: {error}")
            else:
                print(f"::warning::verify-data {label} (not blocking): {type(error).__name__}: {error}")

    if "feed" in parts and (data / "uk_lightning.geojson").exists():
        run("feed", lambda: check_feed(data / "uk_lightning.geojson"))
    if "status" in parts and (data / "status.json").exists():
        run("status", lambda: check_status(data / "status.json"))
    if "archive" in parts and (archive_dir / "index.json").exists():
        run("archive", lambda: "archive ok " + json.dumps(history.verify_archive(archive_dir)))
        run("grid", lambda: f"grid ok ({'deep' if args.deep else 'quick'}) "
                            + json.dumps(grid.verify_grids(archive_dir, deep=args.deep)), blocking=args.deep)
    if not parts:
        print("verify-data: no data changes staged; nothing to verify")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
