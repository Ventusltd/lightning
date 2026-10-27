#!/usr/bin/env bash
# Replay of a backfill run's verified dates on top of a newer origin/main
# (called by push-with-replay.sh after a rejected backfill push). Every date in
# $RUNNER_TEMP/backfill-filled.txt is re-ingested from its saved whole-day run
# record ($RUNNER_TEMP/rb-D.json): no EUMETSAT call is repeated. history.py
# backfill refuses a date that is no longer never-searched (for example the
# hourly job has meanwhile archived part of it); such a date is dropped from
# this run's filled list, keeping the hourly job's rows. The backfill status
# block is rewritten from the replayed list, then the archive is verified.
set -euo pipefail
filled="$RUNNER_TEMP/backfill-filled.txt"
kept="$RUNNER_TEMP/backfill-filled.replay.txt"
: > "$kept"
while read -r D; do
  [ -n "$D" ] || continue
  if python history.py backfill --archive-dir data/history --day "$D" \
       --rebuild-record "$RUNNER_TEMP/rb-$D.json" \
       --result-out "$RUNNER_TEMP/bf-$D.json" < /dev/null; then
    echo "$D" >> "$kept"
  else
    echo "::warning::replay: $D is no longer never-searched on main (or was refused); kept main's row"
  fi
done < "$filled"
mv "$kept" "$filled"
python status.py backfill --status data/status.json \
  --plan "$RUNNER_TEMP/backfill-plan.txt" --filled "$filled" \
  --stopped "${STOPPED:-completed}" --error "${ERROR:-}" --archive-dir data/history
python history.py verify --archive-dir data/history
