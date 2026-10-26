-- Views over the Parquet store. rollups.py substitutes {root} (the store
-- directory) and {flashes_source} (the flash Parquet glob, or an empty typed
-- relation when the archive has no flashes yet). Everything is UTC.
--
-- Coverage contract, applied by every file in sql/:
--   * coverage none (never searched) and shard_lost read UNKNOWN: their counts
--     and hours_observed are NULL. They are never coalesced to 0.
--   * a count of 0 is only produced for time that was searched (count_state
--     'observed'), where it genuinely means no flash was archived.
--   * every count travels with hours_observed / hours_expected or a coverage state.
SET TimeZone = 'UTC';

CREATE OR REPLACE VIEW flashes AS
SELECT id, time_utc, lon, lat, source_product, observation_kind, ingest_run_id
FROM {flashes_source};

CREATE OR REPLACE VIEW coverage_days AS
SELECT * FROM read_parquet('{root}/coverage/days.parquet');

CREATE OR REPLACE VIEW scan_windows AS
SELECT * FROM read_parquet('{root}/coverage/scan_windows.parquet');

CREATE OR REPLACE VIEW coverage_slots AS
SELECT * FROM read_parquet('{root}/coverage/slots/year=*/month=*/*.parquet', hive_partitioning = false);

COMMENT ON VIEW flashes IS
  'EUMETSAT MTG LI L2-LFL optical lightning flashes over the UK box. Satellite optical detections: not ground strikes, not damage, not causality.';
COMMENT ON VIEW coverage_days IS
  'One row per UTC date of the archive index. coverage none or shard_lost = UNKNOWN (NULL counts), never 0.';
