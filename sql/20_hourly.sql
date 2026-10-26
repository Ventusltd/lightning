-- rollups/hourly: UTC hourly optical flash counts joined to coverage.
-- scanned_seconds = overlap of the hour with the merged successful search
-- windows. An hour with archived flashes but no recorded window (seed data)
-- is 'flash_proven': 600 s per distinct 10-minute slot its flashes occupy, a
-- lower bound. Hours of a shard_lost day, and never-searched hours, are
-- UNKNOWN: flash_count and hours_observed are NULL, never 0.
CREATE OR REPLACE VIEW hourly_optical_flashes AS
WITH bounds AS (
  SELECT min(date) AS first_day, max(date) AS last_day FROM coverage_days
),
hours AS (
  SELECT unnest(generate_series(
           CAST(first_day AS TIMESTAMPTZ),
           CAST(last_day AS TIMESTAMPTZ) + INTERVAL 23 HOUR,
           INTERVAL 1 HOUR)) AS hour_utc
  FROM bounds
),
window_seconds AS (
  SELECT h.hour_utc,
         sum(epoch(least(w.end_utc, h.hour_utc + INTERVAL 1 HOUR)) - epoch(greatest(w.start_utc, h.hour_utc))) AS s
  FROM hours h
  JOIN scan_windows w
    ON w.start_utc < h.hour_utc + INTERVAL 1 HOUR AND w.end_utc > h.hour_utc
  GROUP BY 1
),
per_hour AS (
  SELECT date_trunc('hour', time_utc) AS hour_utc,
         count(*) AS n,
         count(DISTINCT time_bucket(INTERVAL 10 MINUTE, time_utc - INTERVAL 9 SECOND)) AS slots
  FROM flashes GROUP BY 1
),
joined AS (
  SELECT h.hour_utc,
         CAST(h.hour_utc AS DATE) AS date,
         d.coverage AS day_coverage,
         CAST(round(coalesce(ws.s, 0)) AS INTEGER) AS window_s,  -- seconds; 0 here means "no window", resolved below
         p.n, p.slots
  FROM hours h
  JOIN coverage_days d ON d.date = CAST(h.hour_utc AS DATE)
  LEFT JOIN window_seconds ws USING (hour_utc)
  LEFT JOIN per_hour p USING (hour_utc)
),
resolved AS (
  SELECT *,
    CASE
      WHEN day_coverage = 'shard_lost' THEN NULL
      WHEN window_s > 0 THEN least(window_s, 3600)
      WHEN n > 0 THEN least(600 * slots, 3600)
      ELSE NULL
    END AS scanned_seconds,
    CASE
      WHEN day_coverage = 'shard_lost' THEN NULL
      WHEN window_s > 0 THEN 'scan_window'
      WHEN n > 0 THEN 'flash_proven'
    END AS basis
  FROM joined
)
SELECT
  hour_utc,
  date,
  CASE WHEN day_coverage = 'shard_lost' THEN 'shard_lost'
       WHEN scanned_seconds IS NULL THEN 'none'
       WHEN scanned_seconds >= 3600 THEN 'full'
       ELSE 'partial' END AS coverage,
  CASE WHEN scanned_seconds IS NULL THEN 'UNKNOWN' ELSE 'observed' END AS count_state,
  round(scanned_seconds / 3600.0, 4) AS hours_observed,
  CAST(1 AS DOUBLE) AS hours_expected,
  scanned_seconds,
  basis,
  CASE WHEN scanned_seconds IS NOT NULL THEN coalesce(n, 0) END AS flash_count
FROM resolved
ORDER BY hour_utc;
