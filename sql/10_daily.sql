-- rollups/daily.parquet: UTC daily optical flash counts joined to coverage.
-- The Parquet count is recomputed here and must equal the index count
-- (verify_parquet.py checks it). coalesce(n, 0) is applied ONLY when the day
-- was searched (count_state = 'observed'): there it is a genuine zero.
CREATE OR REPLACE VIEW daily_optical_flashes AS
WITH per_day AS (
  SELECT CAST(time_utc AS DATE) AS date, count(*) AS n
  FROM flashes GROUP BY 1
)
SELECT
  d.date,
  d.coverage,
  d.count_state,
  d.hours_observed,
  d.hours_expected,
  d.scanned_seconds,
  CASE WHEN d.count_state = 'observed' THEN coalesce(p.n, 0) END AS flash_count,
  d.sealed
FROM coverage_days d
LEFT JOIN per_day p USING (date)
ORDER BY d.date;
