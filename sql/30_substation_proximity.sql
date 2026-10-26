-- rollups/substation_optical_flash_proximity*.parquet
--
-- OPTICAL-FLASH PROXIMITY: the number of satellite optical lightning flashes
-- whose reported location lies within 5 / 10 / 20 km (great-circle, haversine,
-- R = 6371.0088 km) of a GB transmission connection point's joined map
-- location. These are NOT ground strikes, NOT confirmed strikes, NOT
-- electrical faults and NOT damage. LI location accuracy is several km
-- (about 4.5 km at nadir, coarser at UK latitudes), so the 5 km ring is at
-- or below the sensor's spatial resolution.
--
-- The substations relation is registered by rollups.py from
-- sources/gb-transmission-substations.v1.json (pinned by SHA-256).
-- Counts cover only searched time: hours_observed / hours_expected travel with
-- every row, and count_state says 'lower_bound_partial_coverage' whenever any
-- day of the archive range is never-searched, partial or shard_lost.
CREATE OR REPLACE VIEW substation_optical_flash_pairs AS
SELECT s.site_code,
       f.time_utc,
       2 * 6371.0088 * asin(sqrt(
         pow(sin(radians(f.lat - s.lat) / 2), 2) +
         cos(radians(s.lat)) * cos(radians(f.lat)) * pow(sin(radians(f.lon - s.lon) / 2), 2)
       )) AS distance_km
FROM substations s
JOIN flashes f
  ON f.lat BETWEEN s.lat - 0.19 AND s.lat + 0.19
 AND f.lon BETWEEN s.lon - 0.19 / cos(radians(s.lat)) AND s.lon + 0.19 / cos(radians(s.lat));

CREATE OR REPLACE VIEW archive_coverage AS
SELECT
  count(*) AS days_in_range,
  count(*) FILTER (WHERE coverage = 'full') AS days_full,
  count(*) FILTER (WHERE coverage = 'partial') AS days_partial,
  count(*) FILTER (WHERE coverage = 'none') AS days_never_searched,
  count(*) FILTER (WHERE coverage = 'shard_lost') AS days_shard_lost,
  CAST(round(coalesce(sum(hours_observed), 0), 4) AS DOUBLE) AS hours_observed,  -- sum over searched days only; UNKNOWN days add nothing
  CAST(24 * count(*) AS DOUBLE) AS hours_expected
FROM coverage_days;

CREATE OR REPLACE VIEW substation_optical_flash_proximity AS
WITH near AS (
  SELECT site_code,
         count(*) FILTER (WHERE distance_km <= 5)  AS optical_flash_proximity_5km,
         count(*) FILTER (WHERE distance_km <= 10) AS optical_flash_proximity_10km,
         count(*) FILTER (WHERE distance_km <= 20) AS optical_flash_proximity_20km,
         min(time_utc) FILTER (WHERE distance_km <= 20) AS first_flash_within_20km_utc,
         max(time_utc) FILTER (WHERE distance_km <= 20) AS last_flash_within_20km_utc
  FROM substation_optical_flash_pairs
  GROUP BY 1
)
SELECT
  s.site_code, s.name, s.transmission_owner, s.max_kv, s.lon, s.lat, s.matched_by,
  -- 0 is a genuine zero over the searched hours only; it is never a claim about unsearched time.
  coalesce(n.optical_flash_proximity_5km, 0)  AS optical_flash_proximity_5km,
  coalesce(n.optical_flash_proximity_10km, 0) AS optical_flash_proximity_10km,
  coalesce(n.optical_flash_proximity_20km, 0) AS optical_flash_proximity_20km,
  n.first_flash_within_20km_utc,
  n.last_flash_within_20km_utc,
  c.hours_observed, c.hours_expected,
  c.days_full, c.days_partial, c.days_never_searched, c.days_shard_lost,
  CASE WHEN c.hours_observed >= c.hours_expected THEN 'complete'
       ELSE 'lower_bound_partial_coverage' END AS count_state
FROM substations s
CROSS JOIN archive_coverage c
LEFT JOIN near n USING (site_code)
ORDER BY s.site_code;

CREATE OR REPLACE VIEW substation_optical_flash_proximity_daily AS
SELECT
  p.site_code,
  CAST(p.time_utc AS DATE) AS date,
  d.coverage, d.count_state, d.hours_observed, d.hours_expected,
  count(*) FILTER (WHERE p.distance_km <= 5)  AS optical_flash_proximity_5km,
  count(*) FILTER (WHERE p.distance_km <= 10) AS optical_flash_proximity_10km,
  count(*) FILTER (WHERE p.distance_km <= 20) AS optical_flash_proximity_20km
FROM substation_optical_flash_pairs p
JOIN coverage_days d ON d.date = CAST(p.time_utc AS DATE)
WHERE p.distance_km <= 20
GROUP BY ALL
ORDER BY p.site_code, date;

COMMENT ON VIEW substation_optical_flash_proximity IS
  'Optical-flash proximity: satellite optical flashes within 5/10/20 km of a transmission connection point. Not ground strikes, not confirmed strikes, not electrical faults, not damage. Counts cover searched hours only (hours_observed/hours_expected).';
COMMENT ON VIEW substation_optical_flash_proximity_daily IS
  'Optical-flash proximity per site and UTC day, only rows with at least one flash within 20 km. A missing row means 0 on a searched day and UNKNOWN on an unsearched or shard_lost day: join coverage_days.';
