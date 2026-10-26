/**
 * history-window.mjs — standalone UTC date-window client for the UK lightning
 * archive (index v1 or v2 + UTC day shards) and the live 7-day feed.
 *
 * Modelled on GlobalGrid2050 UK Energy Tracker V6 loading principles (named
 * periods, previous/next navigation, tiered loading, Promise caches) without
 * any of its UI. NO DOM, CSS, window.fetch rewrite or GridAtlas mutation.
 *
 * Rules implemented (see docs/HISTORY_ONE_YEAR.md):
 * - accepts uk-lightning-history-index.v1 and .v2; v1 is normalised to v2 rows;
 * - budget BEFORE fan-out from index bytes/count; LRU of 8 shards in memory;
 * - cache 'default' for sealed shards, 'no-cache' for the index and unsealed;
 * - window end clamped to now; start clamped to the retention cutoff and
 *   flagged (never thrown); windows entirely before the archive are a state;
 * - shards fetched with allSettled semantics: failures are named per day and
 *   index counts are still returned;
 * - tier A: the live feed for 12h–7d windows ending now (one request);
 * - coverage per day and per window from scan_windows or day rows;
 * - the nine UI states of the time-window UX specification.
 *
 * EUMETSAT LI optical flashes are NOT confirmed cloud-to-ground strikes.
 * Missing days are UNKNOWN, never zero.
 */
const DAY = 86400000;
const HOUR = 3600000;

export const PERIODS = Object.freeze({
  '12h': '12 hours', '24h': '24 hours', '48h': '48 hours',
  '7d': '7 days', '30d': '30 days', '3m': '3 months',
  '6m': '6 months', '12m': '1 year', custom: 'Custom UTC dates'
});
const FIXED = Object.freeze({ '12h': 12 * HOUR, '24h': DAY, '48h': 2 * DAY, '7d': 7 * DAY, '30d': 30 * DAY });
const MONTHS = Object.freeze({ '3m': 3, '6m': 6, '12m': 12 });

export const STATES = Object.freeze({
  OFF: 'OFF', LOADING: 'LOADING', READY_DETAIL: 'READY·DETAIL', READY_SUMMARY: 'READY·SUMMARY',
  EMPTY_COVERED: 'EMPTY-COVERED', PARTIAL: 'PARTIAL', UNKNOWN: 'UNKNOWN',
  BEFORE_ARCHIVE: 'BEFORE-ARCHIVE', FAIL: 'FAIL'
});
export const STATE_COLOURS = Object.freeze({
  [STATES.OFF]: '#555', [STATES.LOADING]: '#888', [STATES.READY_DETAIL]: '#00ff88',
  [STATES.READY_SUMMARY]: '#00ff88', [STATES.EMPTY_COVERED]: '#00ff88', [STATES.PARTIAL]: '#ffcc00',
  [STATES.UNKNOWN]: '#ff8800', [STATES.BEFORE_ARCHIVE]: '#ff8800', [STATES.FAIL]: '#ff4444'
});
export const DEFAULTS = Object.freeze({
  indexUrl: 'https://raw.githubusercontent.com/Ventusltd/lightning/main/data/history/index.json',
  feedUrl: 'https://raw.githubusercontent.com/Ventusltd/lightning/main/data/uk_lightning.geojson',
  maxDetailDays: 31, maxMapPoints: 40000, maxShardCache: 8, concurrency: 4,
  byteBudget: 32_000_000, countBudget: 40000, bytesPerFeatureEstimate: 530,
  staleAfterMs: 4 * HOUR, feedRetentionDays: 7, feedLatestToleranceMs: 15 * 60000,
  lookbackMaxHours: 48, timeoutMs: 15000
});
export const OBSERVATION_SENTENCE = 'satellite optical lightning flashes, not ground strikes';
export const FOOTER = 'Times UTC · Optical flashes seen by MTG Lightning Imager (EUMETSAT L2-LFL), not confirmed ground strikes · Missing days are unknown, not zero · UK box −11.0,49.5 → 3.0,61.5';

// ---------------------------------------------------------------- windows --
function utc(v, name) {
  const d = v instanceof Date ? new Date(v.getTime()) : new Date(v);
  if (!Number.isFinite(d.getTime())) throw new RangeError(name + ' must be a valid UTC date');
  return d;
}
function minusMonths(d, count) {
  const first = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - count, 1,
    d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds()));
  const length = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
  first.setUTCDate(Math.min(d.getUTCDate(), length));
  return first;
}
const ymd = (ms) => new Date(ms).toISOString().slice(0, 10);
const dayStart = (date) => Date.parse(date + 'T00:00:00Z');

/** All windows are UTC and [start, end) so midnight events cannot double count.
 *  When `now` is given the end is clamped to it (never a future window). */
export function selectUtcWindow({ period = '24h', end = new Date(), start = null, now = null } = {}) {
  if (!Object.hasOwn(PERIODS, period)) throw new RangeError('Unsupported period: ' + period);
  let e = utc(end, 'end');
  let clampedEnd = false;
  if (now !== null && now !== undefined) {
    const n = utc(now, 'now');
    if (e > n) { e = n; clampedEnd = true; }
  }
  const s = period === 'custom' ? utc(start, 'start')
    : MONTHS[period] ? minusMonths(e, MONTHS[period]) : new Date(e.getTime() - FIXED[period]);
  if (s >= e) throw new RangeError('Start must precede end');
  if (s < minusMonths(e, 12)) throw new RangeError('Maximum archive lookback is one calendar year');
  return Object.freeze({ period, start: s.toISOString(), end: e.toISOString(), timezone: 'UTC', bounds: '[start,end)', clampedEnd });
}
/** Date-only input includes the selected end day in full (UTC). */
export function selectUtcDates(fromYmd, toYmd, { now = null } = {}) {
  for (const x of [fromYmd, toYmd]) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(x)) throw new RangeError('Date must be YYYY-MM-DD');
    if (utc(x + 'T00:00:00Z', 'date').toISOString().slice(0, 10) !== x) throw new RangeError('Invalid calendar date');
  }
  return selectUtcWindow({ period: 'custom', start: fromYmd + 'T00:00:00Z', end: new Date(Date.parse(toYmd + 'T00:00:00Z') + DAY), now });
}
export function previousUtcWindow(w) {
  const e = utc(w.start, 'previous end');
  return w.period === 'custom'
    ? selectUtcWindow({ period: 'custom', end: e, start: new Date(e.getTime() - (Date.parse(w.end) - Date.parse(w.start))) })
    : selectUtcWindow({ period: w.period, end: e });
}
export function nextUtcWindow(w, now = new Date()) {
  const end = Math.min(Date.parse(w.end) + (Date.parse(w.end) - Date.parse(w.start)), utc(now, 'now').getTime());
  if (end <= Date.parse(w.end)) return w;
  return w.period === 'custom'
    ? selectUtcWindow({ period: 'custom', end: new Date(end), start: new Date(end - (Date.parse(w.end) - Date.parse(w.start))) })
    : selectUtcWindow({ period: w.period, end: new Date(end) });
}
/** Clamp a window to [cutoff, now). Never throws for the retention edge: it flags. */
export function clampWindow(w, { cutoffMs = -Infinity, nowMs = Infinity } = {}) {
  let start = Date.parse(w.start), end = Date.parse(w.end);
  const result = { clampedStart: false, clampedEnd: false, beforeArchive: false, afterNow: false, window: w };
  if (end > nowMs) { end = nowMs; result.clampedEnd = true; }
  if (start >= nowMs) { result.afterNow = true; return result; }
  if (end <= cutoffMs) { result.beforeArchive = true; return result; }
  if (start < cutoffMs) { start = cutoffMs; result.clampedStart = true; }
  result.window = Object.freeze({ ...w, start: new Date(start).toISOString(), end: new Date(end).toISOString(), clampedEnd: w.clampedEnd || result.clampedEnd });
  return result;
}
/** PREV is possible while the previous window would still end after the cutoff. */
export function canGoPrevious(w, index) {
  const cutoff = index?.cutoffMs ?? dayStart(index?.cutoff_date_utc || '1970-01-01');
  return Date.parse(w.start) > cutoff;
}
export function canGoNext(w, now = Date.now()) {
  return Date.parse(w.end) < utc(now, 'now').getTime();
}

// ------------------------------------------------------------------ index --
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const DAY_PATH_RE = /^days\/\d{4}\/\d{2}\/\d{2}\.json$/;
const PART_PATH_RE = /^days\/\d{4}\/\d{2}\/\d{2}\/\d{2}\.json$/;
const SHA_RE = /^[0-9a-f]{64}$/;
const isCount = (n) => Number.isSafeInteger(n) && n >= 0;

function validateV1Row(row) {
  if (!DATE_RE.test(row.date) || !DAY_PATH_RE.test(row.path) || !isCount(row.count))
    throw new TypeError('Invalid dated lightning history shard (v1): ' + (row && row.date));
}
function validateV2Row(row) {
  const bad = (why) => { throw new TypeError('Invalid lightning history row ' + (row && row.date) + ': ' + why); };
  if (!row || !DATE_RE.test(row.date)) bad('date');
  if (!['none', 'partial', 'full'].includes(row.coverage)) bad('coverage');
  if (!Number.isSafeInteger(row.scanned_seconds) || row.scanned_seconds < 0 || row.scanned_seconds > 86400) bad('scanned_seconds');
  if (row.coverage === 'none' ? row.count !== null : !isCount(row.count)) bad('count must be null iff coverage none');
  if (row.count === 0 && row.scanned_seconds <= 0) bad('count 0 needs scanned time');
  if (row.path !== null && typeof row.path !== 'undefined' && !DAY_PATH_RE.test(row.path)) bad('path');
  if ((row.count === null || row.count === 0) && row.path) bad('path must be null without flashes');
  if (row.count > 0 && !row.path && !(Array.isArray(row.parts) && row.parts.length)) bad('flashes without path or parts');
  if (row.bytes !== null && typeof row.bytes !== 'undefined' && !isCount(row.bytes)) bad('bytes');
  if (typeof row.sealed !== 'boolean') bad('sealed');
  if (row.sha256 !== null && typeof row.sha256 !== 'undefined' && !SHA_RE.test(row.sha256)) bad('sha256');
  for (const part of row.parts || []) {
    if (!PART_PATH_RE.test(part.path) || !isCount(part.count) || !isCount(part.bytes)) bad('part ' + (part && part.path));
  }
}
/** Accept v1 or v2 and return a v2-shaped, enriched index (never mutates the input). */
export function normaliseIndex(raw, { lookbackMaxHours = DEFAULTS.lookbackMaxHours } = {}) {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.days) || raw.observation !== OBSERVATION_SENTENCE)
    throw new TypeError('Invalid historical lightning index');
  const generatedMs = Date.parse(raw.generated_utc);
  if (!Number.isFinite(generatedMs)) throw new TypeError('Lightning index generated_utc is missing');
  if (!DATE_RE.test(raw.cutoff_date_utc || '')) throw new TypeError('Lightning archive cutoff is missing');
  const scanWindows = (raw.scan_windows || []).map((w) => ({ start: Date.parse(w.start_utc), end: Date.parse(w.end_utc), runs: Number(w.successful_runs) || 1 }))
    .filter((w) => Number.isFinite(w.start) && Number.isFinite(w.end) && w.end > w.start).sort((a, b) => a.start - b.start);
  const cutoffMs = dayStart(raw.cutoff_date_utc);
  const todayMs = dayStart(ymd(generatedMs));
  const lookbackMax = Number(raw.collection_lookback_hours_max) || lookbackMaxHours;
  let days, schemaVersion;
  if (raw.schema === 'uk-lightning-history-index.v2') {
    schemaVersion = 2;
    raw.days.forEach(validateV2Row);
    days = raw.days.map((r) => ({ ...r }));
  } else if (raw.schema === 'uk-lightning-history-index.v1') {
    schemaVersion = 1;
    raw.days.forEach(validateV1Row);
    const byDate = new Map(raw.days.map((r) => [r.date, r]));
    days = [];
    for (let t = cutoffMs; t <= todayMs; t += DAY) {
      const date = ymd(t);
      const old = byDate.get(date);
      let scanned = 0;
      for (const w of scanWindows) scanned += Math.max(0, Math.min(w.end, t + DAY) - Math.max(w.start, t)) / 1000;
      scanned = Math.min(86400, Math.round(scanned));
      if (old && scanned <= 0) scanned = 600; // flashes prove at least one examined slot
      const coverage = scanned <= 0 ? 'none' : scanned >= 86400 ? 'full' : 'partial';
      days.push({
        date, coverage, scanned_seconds: scanned, count: coverage === 'none' ? null : (old ? old.count : 0),
        bytes: null, path: old ? old.path : null,
        sealed: coverage === 'full' || generatedMs >= t + DAY + lookbackMax * HOUR,
        first_observation_utc: old?.first_observation_utc || null, last_observation_utc: old?.last_observation_utc || null, sha256: null
      });
    }
  } else {
    throw new TypeError('Unsupported lightning index schema: ' + raw.schema);
  }
  const lastScanEndMs = scanWindows.length ? Math.max(...scanWindows.map((w) => w.end)) : null;
  const coverageStartMs = Date.parse(raw.coverage_start_utc || '') || (scanWindows.length ? Math.min(...scanWindows.map((w) => w.start)) : null);
  const intervalHours = Number(raw.collection_interval_hours) || 3;
  const nextDueMs = Date.parse(raw.next_update_due_utc || '') || generatedMs + (intervalHours + 1) * HOUR;
  return Object.freeze({
    ...raw, schemaVersion, days, dayMap: new Map(days.map((r) => [r.date, r])), scanWindows,
    generatedMs, nextDueMs, cutoffMs, todayMs, lastScanEndMs, coverageStartMs,
    archiveFirstEnabledMs: Date.parse(raw.archive_first_enabled_utc || '') || null, lookbackMaxHours: lookbackMax
  });
}
export function isStale(index, nowMs = Date.now(), staleAfterMs = DEFAULTS.staleAfterMs) {
  return nowMs - index.generatedMs > staleAfterMs || nowMs > index.nextDueMs;
}

// --------------------------------------------------------------- coverage --
function overlap(aStart, aEnd, bStart, bEnd) { return Math.max(0, Math.min(aEnd, bEnd) - Math.max(aStart, bStart)); }
/** Rows of the index whose UTC date intersects the window. */
export function chosenDays(index, w) {
  const start = Date.parse(w.start), end = Date.parse(w.end);
  return index.days.filter((row) => { const from = dayStart(row.date); return from < end && from + DAY > start; });
}
/** Coverage of one UTC date: what the index knows, never a guess. */
export function dayCoverage(index, date) {
  const row = index.dayMap.get(date);
  if (!row) return { date, coverage: 'none', scanned_seconds: 0, count: null, known: false };
  return { ...row, known: true };
}
/** Searched fraction of a window from scan_windows, with day rows as the floor
 *  (rows carry the slots proven by archived flashes when no window was recorded). */
export function windowCoverage(index, w) {
  const ws = Date.parse(w.start), we = Date.parse(w.end);
  const windowSeconds = (we - ws) / 1000;
  let runs = 0, scanStart = null, scanEnd = null;
  for (const sw of index.scanWindows) {
    const o = overlap(ws, we, sw.start, sw.end);
    if (o <= 0) continue;
    runs += sw.runs;
    scanStart = scanStart === null ? Math.max(ws, sw.start) : Math.min(scanStart, Math.max(ws, sw.start));
    scanEnd = scanEnd === null ? Math.min(we, sw.end) : Math.max(scanEnd, Math.min(we, sw.end));
  }
  const days = [];
  const unknownDays = [];
  let searched = 0;
  for (let t = dayStart(ymd(ws)); t < we; t += DAY) {
    const date = ymd(t);
    const o = overlap(ws, we, t, t + DAY);
    if (o <= 0) continue;
    let fromWindows = 0;
    for (const sw of index.scanWindows) fromWindows += overlap(Math.max(ws, t), Math.min(we, t + DAY), sw.start, sw.end);
    const row = index.dayMap.get(date);
    const rowEstimate = row && row.coverage !== 'none' ? row.scanned_seconds * 1000 * (o / DAY) : 0;
    const got = Math.min(o, Math.max(fromWindows, rowEstimate));
    searched += got;
    const coverage = row ? row.coverage : 'none';
    if (coverage === 'none') unknownDays.push(date);
    days.push({ date, overlapSeconds: o / 1000, searchedSeconds: got / 1000, coverage, count: row ? row.count : null, known: !!row });
  }
  const notYetIngestedSeconds = index.lastScanEndMs === null ? windowSeconds : Math.max(0, we - Math.max(ws, index.lastScanEndMs)) / 1000;
  const archiveStart = index.coverageStartMs ?? index.cutoffMs;
  const beforeArchiveSeconds = archiveStart === null ? 0 : Math.max(0, Math.min(we, archiveStart) - ws) / 1000;
  const searchedSeconds = Math.round(searched / 1000);
  return {
    windowSeconds, searchedSeconds, searchedFraction: windowSeconds > 0 ? Math.min(1, searchedSeconds / windowSeconds) : 0,
    runs, scanStart: scanStart === null ? null : new Date(scanStart).toISOString(), scanEnd: scanEnd === null ? null : new Date(scanEnd).toISOString(),
    notYetIngestedSeconds, beforeArchiveSeconds, unknownDays, days
  };
}

// ---------------------------------------------------------------- states --
/** The seven data-derived states (OFF and LOADING belong to the UI). */
export function classifyState(result, { nowMs = Date.now(), staleAfterMs = DEFAULTS.staleAfterMs } = {}) {
  const reasons = [];
  let state;
  if (result.fallback === 'feed') reasons.push('archive unavailable (' + result.indexError + '); live feed shown');
  if (result.indexError && !result.index && result.mode !== 'feed') { state = STATES.FAIL; reasons.push('index: ' + result.indexError); }
  else if (result.beforeArchive) { state = STATES.BEFORE_ARCHIVE; reasons.push('window entirely before the archive'); }
  else if (result.failures && result.failures.length) {
    state = STATES.FAIL;
    reasons.push(result.failures.length + ' of ' + (result.requestedDays || result.failures.length) + ' day files failed');
  } else {
    const c = result.coverage;
    const fraction = c ? c.searchedFraction : 0;
    if (!c || fraction <= 0) { state = STATES.UNKNOWN; reasons.push('0% searched'); }
    else if (fraction < 0.999) {
      state = STATES.PARTIAL;
      if (c.notYetIngestedSeconds > 0) reasons.push('tail not yet ingested');
      if (c.beforeArchiveSeconds > 0) reasons.push('head before archive start');
      if (c.unknownDays.length) reasons.push(c.unknownDays.length + ' dates without a scan window');
      if (!reasons.length) reasons.push('gaps inside the window');
    } else if (result.mode === 'detail' || result.mode === 'feed') {
      state = result.totalMatchingObservations > 0 ? STATES.READY_DETAIL : STATES.EMPTY_COVERED;
    } else {
      state = result.archivedDayCount > 0 ? STATES.READY_SUMMARY : STATES.EMPTY_COVERED;
    }
  }
  const stale = !!(result.index && isStale(result.index, nowMs, staleAfterMs));
  return { state, stale, colour: STATE_COLOURS[state], reasons };
}

// --------------------------------------------------------------- captions --
const pad = (n) => String(n).padStart(2, '0');
export const T = (iso) => { const d = new Date(iso); return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ' UTC'; };
export const Ts = (iso) => T(iso).replace(' UTC', ':' + pad(new Date(iso).getUTCSeconds()) + ' UTC');
export const N = (n) => Number(n).toLocaleString('en-GB');
export const AGO = (ms) => { const m = Math.max(0, Math.round(ms / 60000)); return Math.floor(m / 60) + 'h ' + (m % 60) + 'min'; };
/** The seven caption templates of the UX specification, as plain strings. */
export function captionLines(result, { nowMs = Date.now() } = {}) {
  const w = result.window;
  const lines = { window: '', actual: '', flashes: '', refresh: '', searched: '', warning: '', footer: FOOTER };
  const label = PERIODS[w.period] || w.period;
  lines.window = w.period === 'custom'
    ? 'Window  UTC dates ' + w.start.slice(0, 10) + ' to ' + ymd(Date.parse(w.end) - 1) + ' inclusive = ' + T(w.start) + ' → ' + T(w.end)
    : 'Window  ' + T(w.start) + ' → ' + T(w.end) + ' (' + label + (result.atLatest ? ', latest' : ', end exclusive') + ')';
  if (result.indexError && !result.index && result.mode !== 'feed') {
    lines.warning = 'Archive unavailable: ' + result.indexError + ' at ' + T(new Date(nowMs).toISOString()) + ' — nothing shown (unknown, not zero)';
    return lines;
  }
  if (result.mode === 'feed') {
    const meta = result.feedMetadata || {};
    lines.refresh = 'Refresh live feed ' + (meta.query_end_utc ? T(meta.query_end_utc) : 'unknown') + ' (last collection window ' +
      (meta.query_start_utc ? T(meta.query_start_utc) + ' → ' + T(meta.query_end_utc) : 'unknown') + '; 7-day retention' +
      (result.fallback === 'feed' ? '; archive not yet published: ' + result.indexError : '') + ')';
  }
  if (result.beforeArchive) {
    lines.warning = 'Window predates the archive (began ' + (result.index?.coverage_start_utc || result.index?.archive_first_enabled_utc || '?').slice(0, 10) +
      '; retention from ' + result.index?.cutoff_date_utc + ') — nothing can be shown; not zero lightning';
    return lines;
  }
  if (result.mode === 'detail' || result.mode === 'feed') {
    lines.actual = result.totalMatchingObservations > 0
      ? 'Actual  first ' + Ts(result.firstObservationUtc) + ' · last ' + Ts(result.lastObservationUtc)
      : 'Actual  no archived flashes within this window' + (result.previousObservationUtc
        ? ' · last archived flash before window ' + Ts(result.previousObservationUtc) + ' (' + AGO(Date.parse(w.start) - Date.parse(result.previousObservationUtc)) + ' before start)' : '');
    lines.flashes = 'Flashes ' + N(result.totalMatchingObservations) + ' archived in window · ' + N(result.shownObservations) + ' drawn' +
      (result.truncated ? ' (cap ' + N(result.maxMapPoints) + ': newest drawn, ' + N(result.totalMatchingObservations - result.shownObservations) + ' older not drawn)' : '');
  } else {
    const rows = result.selectedDays.filter((r) => r.first_observation_utc);
    lines.actual = rows.length
      ? 'Actual  on the ' + result.selectedDays.length + ' overlapping UTC dates: first ' + Ts(rows[0].first_observation_utc) + ' · last ' + Ts(rows.at(-1).last_observation_utc) + ' (day-level; may lie outside a mid-day window edge)'
      : 'Actual  no archived flashes on the ' + result.selectedDays.length + ' overlapping UTC dates';
    lines.flashes = 'Flashes ' + N(result.archivedDayCount) + ' archived on ' + result.selectedDays.length + ' overlapping UTC dates · none drawn (summary mode; choose ≤31 days or LOAD POINTS)';
  }
  if (result.index) {
    const age = nowMs - result.index.generatedMs;
    const archive = 'Refresh archive ' + T(result.index.generated_utc) + ' (' + AGO(age) + ' ago; expected every ' + (result.index.collection_interval_hours || 3) + ' h)' + (isStale(result.index, nowMs) ? ' · STALE' : '');
    lines.refresh = lines.refresh ? archive + ' · ' + lines.refresh.replace(/^Refresh /, '') : archive;
  }
  const c = result.coverage;
  if (c) {
    lines.searched = c.searchedFraction > 0
      ? 'Searched EUMETSAT ' + Math.round(c.searchedFraction * 100) + '% of window · ' + c.runs + ' successful runs' + (c.scanStart ? ' · ' + T(c.scanStart) + ' → ' + T(c.scanEnd) : '')
      : 'Searched EUMETSAT 0% of window — no collection run covers these dates';
    const warnings = [];
    if (c.notYetIngestedSeconds > 0) warnings.push('Not yet ingested: last ' + AGO(c.notYetIngestedSeconds * 1000) + ' of window (unknown, not zero)');
    if (c.beforeArchiveSeconds > 0 && result.index) warnings.push('Unknown: ' + w.start.slice(0, 10) + ' to ' + ymd(Math.min(Date.parse(w.end), result.index.coverageStartMs ?? result.index.cutoffMs) - 1) + ' precede archive start ' + (result.index.coverage_start_utc || result.index.cutoff_date_utc).slice(0, 10) + ' (not zero)');
    if (c.unknownDays.length) warnings.push('Unknown: ' + c.unknownDays.length + ' of ' + c.days.length + ' UTC dates have no collection run');
    if (result.failures && result.failures.length) warnings.push(result.failures.length + ' of ' + result.requestedDays + ' day files failed (' + result.failures.map((f) => f.date).join(', ') + '); points incomplete, counts from index');
    lines.warning = warnings.join(' · ');
  }
  return lines;
}
/** One-line honest summary (compatibility with the draft API). */
export function formatHistoryLabel(result, opts = {}) {
  const l = captionLines(result, opts);
  return [l.window, l.actual, l.flashes, l.refresh, l.searched, l.warning].filter(Boolean).join(' · ') + ' · Historical gaps are unknown, not zero lightning';
}

// ----------------------------------------------------------------- client --
class Lru {
  constructor(max) { this.max = max; this.map = new Map(); }
  get(key) { if (!this.map.has(key)) return undefined; const v = this.map.get(key); this.map.delete(key); this.map.set(key, v); return v; }
  set(key, value) {
    if (this.map.has(key)) this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.max) this.map.delete(this.map.keys().next().value);
  }
  delete(key) { this.map.delete(key); }
  get size() { return this.map.size; }
  keys() { return [...this.map.keys()]; }
}
async function readJson(fetchImpl, url, cache, timeoutMs) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const res = await fetchImpl(url, { cache, signal: controller ? controller.signal : undefined });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } finally { if (timer) clearTimeout(timer); }
}
function validateFeature(feature) {
  return feature?.type === 'Feature' && feature.geometry?.type === 'Point' &&
    feature.properties?.observation_kind === 'optical_lightning_flash' && typeof feature.id === 'string' &&
    Number.isFinite(Date.parse(feature.properties?.time_utc));
}
function collect(parts, ws, we) {
  const found = new Map();
  for (const part of parts) for (const feature of part) {
    if (!validateFeature(feature)) continue;
    const t = Date.parse(feature.properties.time_utc);
    if (t < ws || t >= we) continue;
    found.set(feature.id, feature);
  }
  return [...found.values()].sort((a, b) => a.properties.time_utc.localeCompare(b.properties.time_utc) || a.id.localeCompare(b.id));
}
/** Shard files (whole day or the hourly parts overlapping the window) for a row. */
export function shardTargets(row, w) {
  if (!row || !row.count) return [];
  if (row.path) return [{ path: row.path, bytes: row.bytes, count: row.count, sha256: row.sha256, sealed: row.sealed }];
  const ws = Date.parse(w.start), we = Date.parse(w.end), day = dayStart(row.date);
  return (row.parts || []).filter((p) => { const h = day + Number(p.hour) * HOUR; return h < we && h + HOUR > ws; })
    .map((p) => ({ path: p.path, bytes: p.bytes, count: p.count, sha256: p.sha256, sealed: row.sealed }));
}
/** Pre-fetch budget: index bytes (or an estimate from count) and counts, before any shard request. */
export function windowBudget(rows, w, { byteBudget = DEFAULTS.byteBudget, countBudget = DEFAULTS.countBudget, bytesPerFeatureEstimate = DEFAULTS.bytesPerFeatureEstimate } = {}) {
  let bytes = 0, count = 0, requests = 0, estimated = false;
  for (const row of rows) for (const t of shardTargets(row, w)) {
    requests++;
    count += t.count || 0;
    if (Number.isFinite(t.bytes) && t.bytes !== null) bytes += t.bytes; else { bytes += (t.count || 0) * bytesPerFeatureEstimate; estimated = true; }
  }
  const withinBudget = bytes <= byteBudget && count <= countBudget;
  return { bytes, count, requests, estimated, byteBudget, countBudget, withinBudget,
    reason: withinBudget ? null : (count > countBudget ? N(count) + ' indexed flashes exceed ' + N(countBudget) : N(bytes) + ' indexed bytes exceed ' + N(byteBudget)) };
}

/** Makes no UI; a GridAtlas implant can consume only this data contract. */
export function createLightningHistoryClient(options = {}) {
  const o = { ...DEFAULTS, ...options };
  const fetchImpl = o.fetchImpl || fetch;
  const feedFetchImpl = o.feedFetchImpl || fetchImpl;   // the implant uses XHR for the .geojson feed
  const nowMs = () => (typeof o.now === 'function' ? Number(o.now()) : Date.now());
  const shards = new Lru(o.maxShardCache);
  let cachedIndex = null, cachedFeed = null, lastIndex = null;

  function invalidateChanged(index) {
    // Only shards whose row changed (or that are not sealed) are dropped.
    for (const url of shards.keys()) {
      const entry = shards.get(url);
      const rel = url.startsWith(o.indexUrl.replace(/index\.json$/, '')) ? url.slice(o.indexUrl.replace(/index\.json$/, '').length) : url;
      const row = index.days.find((r) => r.path === rel || (r.parts || []).some((p) => p.path === rel));
      const part = row && !row.path ? (row.parts || []).find((p) => p.path === rel) : null;
      const sha = part ? part.sha256 : row?.sha256;
      if (!row || !row.sealed || (sha && entry.sha256 && sha !== entry.sha256) || (sha && !entry.sha256)) shards.delete(url);
    }
  }
  async function index({ refresh = false } = {}) {
    if (!cachedIndex || refresh) {
      cachedIndex = readJson(fetchImpl, o.indexUrl, 'no-cache', o.timeoutMs).then((raw) => normaliseIndex(raw, o)).then((m) => {
        if (lastIndex && lastIndex.generated_utc !== m.generated_utc) invalidateChanged(m);
        lastIndex = m;
        return m;
      }).catch((e) => { cachedIndex = null; throw e; });
    }
    return cachedIndex;
  }
  function shard(target, date) {
    const url = new URL(target.path, o.indexUrl).href;
    const cached = shards.get(url);
    if (cached) return cached.promise;
    const promise = readJson(fetchImpl, url, target.sealed ? 'default' : 'no-cache', o.timeoutMs).then((obj) => {
      if (obj?.schema !== 'uk-lightning-history-day.v1' || obj?.type !== 'FeatureCollection' || !Array.isArray(obj.features) || obj.metadata?.date_utc !== date)
        throw new TypeError('Invalid flash history day ' + date);
      return obj.features;
    }).catch((e) => { shards.delete(url); throw e; });
    shards.set(url, { promise, sha256: target.sha256 || null, sealed: !!target.sealed });
    return promise;
  }
  async function feed({ refresh = false } = {}) {
    if (!cachedFeed || refresh) {
      cachedFeed = readJson(feedFetchImpl, o.feedUrl, 'no-cache', o.timeoutMs).then((obj) => {
        if (obj?.type !== 'FeatureCollection' || !Array.isArray(obj.features)) throw new TypeError('Invalid live lightning feed');
        return obj;
      }).catch((e) => { cachedFeed = null; throw e; });
    }
    return cachedFeed;
  }
  /** allSettled fan-out with bounded concurrency; failures are named per day. */
  async function fetchRows(rows, w) {
    const jobs = [];
    for (const row of rows) for (const t of shardTargets(row, w)) jobs.push({ row, target: t });
    const parts = new Array(jobs.length);
    const failures = [];
    const warnings = [];
    let pos = 0;
    await Promise.all(Array.from({ length: Math.min(o.concurrency, jobs.length) }, async () => {
      while (pos < jobs.length) {
        const i = pos++;
        const { row, target } = jobs[i];
        try {
          parts[i] = await shard(target, row.date);
          if (row.path && Number.isSafeInteger(row.count) && parts[i].length !== row.count)
            warnings.push({ date: row.date, kind: 'count-mismatch', indexCount: row.count, shardCount: parts[i].length });
        } catch (e) {
          parts[i] = [];
          failures.push({ date: row.date, path: target.path, error: e && e.message ? e.message : String(e) });
        }
      }
    }));
    return { parts, failures, warnings, requested: jobs.length };
  }
  function feedWindowOk(w, t) {
    const span = Date.parse(w.end) - Date.parse(w.start);
    return span <= o.feedRetentionDays * DAY && t - Date.parse(w.end) <= o.feedLatestToleranceMs;
  }
  async function fromFeed(w, t, m, indexError) {
    const f = await feed();
    const meta = f.metadata || {};
    const events = collect([f.features], Date.parse(w.start), Date.parse(w.end));
    const shown = events.length > o.maxMapPoints ? events.slice(-o.maxMapPoints) : events;
    const firstFeed = f.features.length ? Date.parse(f.features[0].properties.time_utc) : null;
    const feedTruncated = Number(meta.total_before_display_cap) > f.features.length;
    let coverage = m ? windowCoverage(m, w) : null;
    if (!coverage) {
      // Only the last search window is known from the feed itself.
      const qs = Date.parse(meta.query_start_utc || ''), qe = Date.parse(meta.scan_end_utc || meta.query_end_utc || '');
      const ws = Date.parse(w.start), we = Date.parse(w.end);
      const searched = Number.isFinite(qs) && Number.isFinite(qe) ? overlap(ws, we, qs, qe) / 1000 : 0;
      coverage = { windowSeconds: (we - ws) / 1000, searchedSeconds: searched, searchedFraction: Math.min(1, searched / ((we - ws) / 1000)),
        runs: searched > 0 ? 1 : 0, scanStart: searched > 0 ? new Date(Math.max(ws, qs)).toISOString() : null, scanEnd: searched > 0 ? new Date(Math.min(we, qe)).toISOString() : null,
        notYetIngestedSeconds: Number.isFinite(qe) ? Math.max(0, we - Math.max(ws, qe)) / 1000 : (we - ws) / 1000, beforeArchiveSeconds: 0, unknownDays: [], days: [], fromFeedMetadataOnly: true };
    }
    const result = {
      mode: 'feed', source: 'live-feed', window: w, index: m, indexError: indexError || null, fallback: indexError ? 'feed' : null,
      feedMetadata: meta, feedTruncated, feedFirstObservationUtc: firstFeed === null ? null : new Date(firstFeed).toISOString(),
      selectedDays: m ? chosenDays(m, w) : [], archivedDayCount: null, coverage, failures: [], warnings: [], requestedDays: 0,
      totalMatchingObservations: events.length, shownObservations: shown.length, truncated: events.length > shown.length, maxMapPoints: o.maxMapPoints,
      firstObservationUtc: events[0]?.properties.time_utc || null, lastObservationUtc: events.at(-1)?.properties.time_utc || null,
      geojson: { type: 'FeatureCollection', features: shown }, atLatest: true, clamped: { start: false, end: false },
      availabilityNote: 'live feed, 7-day retention; coverage of hours outside the last search window is unknown unless the archive index says otherwise'
    };
    // Dates the 40k feed cap removed are completed from day shards when the index is present.
    if (m && feedTruncated && firstFeed !== null && Date.parse(w.start) < firstFeed) {
      const rows = chosenDays(m, { start: w.start, end: new Date(firstFeed).toISOString() }).filter((r) => r.count > 0);
      const got = await fetchRows(rows, w);
      const merged = collect([shown, ...got.parts], Date.parse(w.start), Date.parse(w.end));
      const shown2 = merged.length > o.maxMapPoints ? merged.slice(-o.maxMapPoints) : merged;
      Object.assign(result, { failures: got.failures, warnings: got.warnings, requestedDays: rows.length, totalMatchingObservations: merged.length,
        shownObservations: shown2.length, truncated: merged.length > shown2.length, firstObservationUtc: merged[0]?.properties.time_utc || null,
        lastObservationUtc: merged.at(-1)?.properties.time_utc || null, geojson: { type: 'FeatureCollection', features: shown2 } });
    }
    result.status = classifyState(result, { nowMs: t, staleAfterMs: o.staleAfterMs });
    return result;
  }
  /**
   * Load a window. Default tier: detail for ≤ maxDetailDays when the budget
   * allows, else overview (index only). `consentPoints` loads the newest
   * shards up to maxMapPoints when the budget refuses detail (LOAD POINTS).
   */
  async function loadWindow(window, { detail = null, refreshIndex = false, tier = 'auto', consentPoints = false } = {}) {
    const t = nowMs();
    const requested = selectUtcWindow({ period: window.period, start: window.start, end: window.end, now: t });
    let m = null, indexError = null;
    try { m = await index({ refresh: refreshIndex }); } catch (e) { indexError = e && e.message ? e.message : String(e); }
    const atLatest = t - Date.parse(requested.end) <= o.feedLatestToleranceMs;
    if (tier === 'feed' || (!m && o.feedUrl && feedWindowOk(requested, t))) {
      if (tier === 'feed' && !feedWindowOk(requested, t)) throw new RangeError('The live feed covers only the last ' + o.feedRetentionDays + ' days ending now');
      try {
        return await fromFeed(requested, t, m, indexError);
      } catch (e) {
        if (!indexError) throw e;
        const both = new Error('History index unavailable: ' + indexError + '; live feed unavailable: ' + (e && e.message ? e.message : e));
        both.indexError = indexError; both.feedError = e && e.message ? e.message : String(e);
        throw both;
      }
    }
    if (!m) { const e = new Error('History index unavailable: ' + indexError); e.indexError = indexError; throw e; }
    const clamp = clampWindow(requested, { cutoffMs: m.cutoffMs, nowMs: t });
    if (clamp.afterNow) throw new RangeError('Window starts in the future');
    const w = clamp.window;
    const base = {
      mode: 'overview', window: w, requestedWindow: requested, index: m, indexError: null, atLatest,
      clamped: { start: clamp.clampedStart, end: clamp.clampedEnd || requested.clampedEnd }, beforeArchive: clamp.beforeArchive,
      selectedDays: [], archivedDayCount: 0, unknownDayCount: 0, dailyCounts: [], coverage: null, budget: null, failures: [], warnings: [],
      requestedDays: 0, maxMapPoints: o.maxMapPoints, availabilityNote: m.availability_note || null, scanWindowNote: m.scan_window_note || null,
      canDrillDown: true, canLoadPoints: false, detailReason: null
    };
    if (clamp.beforeArchive) {
      base.status = classifyState(base, { nowMs: t, staleAfterMs: o.staleAfterMs });
      return base;
    }
    const rows = chosenDays(m, w);
    const known = rows.filter((r) => r.count !== null);
    Object.assign(base, {
      selectedDays: rows, archivedDayCount: known.reduce((n, r) => n + r.count, 0), unknownDayCount: rows.length - known.length,
      dailyCounts: rows.map((r) => ({ date: r.date, count: r.count, coverage: r.coverage, scanned_seconds: r.scanned_seconds })),
      coverage: windowCoverage(m, w), budget: windowBudget(rows, w, o)
    });
    const days = (Date.parse(w.end) - Date.parse(w.start)) / DAY;
    const wantDetail = detail === null ? days <= o.maxDetailDays : detail;
    if (!wantDetail || days > o.maxDetailDays) {
      base.detailReason = days > o.maxDetailDays ? 'Choose at most ' + o.maxDetailDays + ' days for exact point positions' : 'Long date ranges load small daily summaries until the user selects details';
      base.status = classifyState(base, { nowMs: t, staleAfterMs: o.staleAfterMs });
      return base;
    }
    let rowsToFetch = rows.filter((r) => r.count > 0);
    let loadedDays = rowsToFetch.map((r) => r.date), skippedDays = [];
    if (!base.budget.withinBudget) {
      if (!consentPoints) {
        base.detailReason = 'Index sum exceeds the detail budget (' + base.budget.reason + '); summary mode until LOAD POINTS';
        base.canLoadPoints = true;
        base.status = classifyState(base, { nowMs: t, staleAfterMs: o.staleAfterMs });
        return base;
      }
      // LOAD POINTS: newest days first until the map cap is reached.
      const newestFirst = [...rowsToFetch].sort((a, b) => b.date.localeCompare(a.date));
      let running = 0; rowsToFetch = [];
      for (const r of newestFirst) { if (running >= o.maxMapPoints) { skippedDays.push(r.date); continue; } rowsToFetch.push(r); running += r.count; }
      loadedDays = rowsToFetch.map((r) => r.date);
    }
    const got = await fetchRows(rowsToFetch, w);
    const events = collect(got.parts, Date.parse(w.start), Date.parse(w.end));
    const shown = events.length > o.maxMapPoints ? events.slice(-o.maxMapPoints) : events;
    let previousObservationUtc = null;
    if (!events.length) {
      const before = m.days.filter((r) => r.last_observation_utc && Date.parse(r.last_observation_utc) < Date.parse(w.start));
      previousObservationUtc = before.length ? before.at(-1).last_observation_utc : null;
    }
    const result = {
      ...base, mode: 'detail', source: 'archive', failures: got.failures, warnings: got.warnings, requestedDays: got.requested,
      loadedDays, skippedDays, consentPoints, totalMatchingObservations: events.length, shownObservations: shown.length,
      truncated: events.length > shown.length || skippedDays.length > 0, firstObservationUtc: events[0]?.properties.time_utc || null,
      lastObservationUtc: events.at(-1)?.properties.time_utc || null, previousObservationUtc, geojson: { type: 'FeatureCollection', features: shown }
    };
    result.status = classifyState(result, { nowMs: t, staleAfterMs: o.staleAfterMs });
    return result;
  }
  return Object.freeze({
    index, feed, loadWindow, periods: PERIODS, states: STATES,
    cacheStats: () => ({ shards: shards.size, max: o.maxShardCache, urls: shards.keys() })
  });
}
