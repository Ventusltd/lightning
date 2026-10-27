#!/usr/bin/env bash
# Replay of update-lightning.yml steps 3-4 on top of a newer origin/main (called
# by push-with-replay.sh after a rejected archive push). Inputs come from the
# run-local record in $RUNNER_TEMP; ARCHIVE_OUTCOME, COLLECT_OUTCOME, TRIGGER,
# RUN_ID, RUN_CREATED_UTC and JOB_STARTED_UTC come from the step environment.
# The archive is ingested again only if it succeeded the first time; if the
# replayed ingest or verify fails, the archive is restored and only the
# heartbeat (with archive outcome failure) is committed.
set -euo pipefail
archive=${ARCHIVE_OUTCOME:-skipped}
if [ "$archive" = "success" ]; then
  if python history.py ingest \
       --archive-dir data/history \
       --run-record "$RUNNER_TEMP/lightning-run-record.json" \
       --seed-scan-windows data/seed_scan_windows.json \
       --result-out "$RUNNER_TEMP/lightning-archive-result.json" \
     && python history.py verify --archive-dir data/history; then
    archive=success
  else
    echo "::warning::replayed archive ingest failed on the new main; committing the heartbeat only"
    archive=failure
    if [ -d data/history ]; then
      git checkout -- data/history 2>/dev/null || true
      git clean -fdq -- data/history
    fi
  fi
fi
python status.py write \
  --status data/status.json \
  --run-record "$RUNNER_TEMP/lightning-run-record.json" \
  --archive-result "$RUNNER_TEMP/lightning-archive-result.json" \
  --collect-outcome "${COLLECT_OUTCOME:-failure}" \
  --archive-outcome "$archive" \
  --trigger "${TRIGGER:-}" --run-id "${RUN_ID:-}" \
  --run-created "${RUN_CREATED_UTC:-}" --job-started "${JOB_STARTED_UTC:-}"
