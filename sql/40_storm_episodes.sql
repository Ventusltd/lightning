-- rollups/storm_episodes.parquet: time clusters of optical flashes anywhere in
-- the UK box. Consecutive flashes belong to one episode when they are at most
-- 30 minutes apart AND the whole gap between them lies inside one searched
-- window. A gap that crosses unsearched time always starts a new episode
-- (we cannot know it was quiet), and the edges say whether the 30 minutes
-- before the first and after the last flash were searched: an episode is
-- 'complete' only if both were. An episode is NOT a single storm cell (two
-- simultaneous storms in the box merge) and says nothing about ground strikes.
-- A shard_lost day is never searched time, even inside a scan window: its
-- flashes are gone, so a gap or edge that touches it is unsearched.
CREATE OR REPLACE VIEW storm_episodes AS
WITH lost AS (
  SELECT CAST(date AS TIMESTAMP) AS start_utc, CAST(date AS TIMESTAMP) + INTERVAL 1 DAY AS end_utc
  FROM coverage_days WHERE coverage = 'shard_lost'
),
ordered AS (
  SELECT id, time_utc, lon, lat,
         lag(time_utc) OVER (ORDER BY time_utc, id) AS prev_utc
  FROM flashes
),
marked AS (
  SELECT o.*,
    CASE
      WHEN prev_utc IS NULL THEN 'first'
      WHEN time_utc - prev_utc > INTERVAL 30 MINUTE THEN 'quiet_gap'
      WHEN NOT EXISTS (SELECT 1 FROM scan_windows w
                       WHERE w.start_utc <= o.prev_utc AND w.end_utc >= o.time_utc)
        OR EXISTS (SELECT 1 FROM lost l
                   WHERE l.start_utc < o.time_utc AND l.end_utc > o.prev_utc) THEN 'unsearched_gap'
    END AS break_reason
  FROM ordered o
),
numbered AS (
  SELECT *, sum(CASE WHEN break_reason IS NOT NULL THEN 1 ELSE 0 END)
              OVER (ORDER BY time_utc, id ROWS UNBOUNDED PRECEDING) AS episode_no
  FROM marked
),
episodes AS (
  SELECT episode_no,
         min(time_utc) AS start_utc,
         max(time_utc) AS end_utc,
         count(*) AS flash_count,
         min(lon) AS min_lon, max(lon) AS max_lon, min(lat) AS min_lat, max(lat) AS max_lat,
         round(avg(lon), 4) AS mean_lon, round(avg(lat), 4) AS mean_lat,
         arg_min(break_reason, time_utc) AS start_reason
  FROM numbered GROUP BY 1
),
edged AS (
  SELECT e.*,
    EXISTS (SELECT 1 FROM scan_windows w
            WHERE w.start_utc <= e.start_utc - INTERVAL 30 MINUTE AND w.end_utc >= e.start_utc)
      AND NOT EXISTS (SELECT 1 FROM lost l
                      WHERE l.start_utc < e.start_utc AND l.end_utc > e.start_utc - INTERVAL 30 MINUTE) AS searched_30min_before,
    EXISTS (SELECT 1 FROM scan_windows w
            WHERE w.start_utc <= e.end_utc AND w.end_utc >= e.end_utc + INTERVAL 30 MINUTE)
      AND NOT EXISTS (SELECT 1 FROM lost l
                      WHERE l.start_utc < e.end_utc + INTERVAL 30 MINUTE AND l.end_utc > e.end_utc) AS searched_30min_after
  FROM episodes e
)
SELECT
  'E' || strftime(start_utc, '%Y%m%dT%H%M%S') || '-' || CAST(episode_no AS VARCHAR) AS episode_id,
  start_utc, end_utc,
  CAST(epoch(end_utc) - epoch(start_utc) AS DOUBLE) AS duration_seconds,
  flash_count,
  min_lon, max_lon, min_lat, max_lat, mean_lon, mean_lat,
  start_reason,
  searched_30min_before,
  searched_30min_after,
  CASE WHEN searched_30min_before AND searched_30min_after THEN 'complete'
       ELSE 'edge_unsearched_may_extend' END AS coverage
FROM edged
ORDER BY start_utc;
