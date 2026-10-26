import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PERIODS, STATES, STATE_COLOURS, selectUtcWindow, selectUtcDates, previousUtcWindow, nextUtcWindow, clampWindow,
  canGoPrevious, canGoNext, normaliseIndex, isStale, windowCoverage, dayCoverage, windowBudget, shardTargets,
  classifyState, captionLines, formatHistoryLabel, createLightningHistoryClient
} from '../history-window.mjs';

// Synthetic fixtures live only in this test process (ids start with "TEST-"); nothing is published.
const base = 'https://raw.githubusercontent.com/Ventusltd/lightning/main/data/history/index.json';
const feedUrl = 'https://raw.githubusercontent.com/Ventusltd/lightning/main/data/uk_lightning.geojson';
const NOW = Date.parse('2026-10-26T14:00:00Z');
const OBS = 'satellite optical lightning flashes, not ground strikes';
const flash = (time, id) => ({
  type: 'Feature', id: 'TEST-' + id, geometry: { type: 'Point', coordinates: [-2, 51] },
  properties: { source_product: 'MTG LI L2-LFL', observation_kind: 'optical_lightning_flash', time_utc: time }
});
const shard = (date, features, extra = {}) => ({
  schema: 'uk-lightning-history-day.v1', type: 'FeatureCollection', metadata: { date_utc: date, count: features.length, ...extra }, features
});
const sha = (c) => c.repeat(64);
function v2Row(date, over = {}) {
  return {
    date, coverage: 'none', scanned_seconds: 0, count: null, bytes: null, path: null, sealed: true,
    first_observation_utc: null, last_observation_utc: null, sha256: null, ...over
  };
}
/** Rows for every date between two dates (inclusive), mostly never scanned. */
function rowsBetween(from, to, overrides) {
  const rows = [];
  for (let t = Date.parse(from + 'T00:00:00Z'); t <= Date.parse(to + 'T00:00:00Z'); t += 86400000) {
    const date = new Date(t).toISOString().slice(0, 10);
    rows.push(v2Row(date, overrides[date] || {}));
  }
  return rows;
}
function v2Index(overrides = {}, top = {}) {
  return {
    schema: 'uk-lightning-history-index.v2', generated_utc: '2026-10-26T13:00:00.000Z', collection_interval_hours: 1,
    collection_lookback_hours: 6, collection_lookback_hours_max: 48, next_update_due_utc: '2026-10-26T16:00:00.000Z',
    cutoff_date_utc: '2025-10-26', retention: 'one calendar year, UTC', source: 'EUMETSAT MTG Lightning Imager L2-LFL',
    source_url: 'https://example.invalid', observation: OBS, bounds_wsen: [-11, 49.5, 3, 61.5], max_shard_bytes: 25000000,
    archive_first_enabled_utc: '2026-10-26T13:00:00.000Z', coverage_start_utc: '2026-10-20T22:41:54.847Z',
    last_successful_scan_end_utc: '2026-10-26T13:00:09.000Z', archived_observations: 3,
    first_observation_utc: '2026-10-24T10:00:00Z', last_observation_utc: '2026-10-25T01:00:00Z',
    scan_windows: [{ start_utc: '2026-10-20T22:41:54.847Z', end_utc: '2026-10-22T22:40:09.000Z', successful_runs: 1 },
      { start_utc: '2026-10-24T00:00:00.000Z', end_utc: '2026-10-25T12:00:00.000Z', successful_runs: 5 },
      { start_utc: '2026-10-26T10:05:58.649Z', end_utc: '2026-10-26T13:00:09.000Z', successful_runs: 1 }],
    availability_note: 'Missing archives mean unknown, not no lightning', scan_window_note: 'not a guarantee',
    days: rowsBetween('2025-10-26', '2026-10-26', {
      '2026-10-20': { coverage: 'partial', scanned_seconds: 4685, count: 0 },
      '2026-10-21': { coverage: 'full', scanned_seconds: 86400, count: 0 },
      '2026-10-22': { coverage: 'partial', scanned_seconds: 81609, count: 0 },
      '2026-10-24': { coverage: 'full', scanned_seconds: 86400, count: 2, bytes: 1100, path: 'days/2026/10/24.json', sealed: true,
        first_observation_utc: '2026-10-24T10:00:00Z', last_observation_utc: '2026-10-24T23:59:59Z', sha256: sha('a') },
      '2026-10-25': { coverage: 'partial', scanned_seconds: 43200, count: 1, bytes: 550, path: 'days/2026/10/25.json', sealed: false,
        first_observation_utc: '2026-10-25T01:00:00Z', last_observation_utc: '2026-10-25T01:00:00Z', sha256: sha('b') },
      '2026-10-26': { coverage: 'partial', scanned_seconds: 10451, count: 0, sealed: false },
      ...overrides
    }),
    status: 'archive_partial_until_history_is_genuinely_collected_or_backfilled', ...top
  };
}
function v1Index() {
  return {
    schema: 'uk-lightning-history-index.v1', generated_utc: '2026-10-26T13:00:00Z', observation: OBS, cutoff_date_utc: '2025-10-26',
    archive_first_enabled_utc: '2026-10-26T13:00:00Z',
    days: [{ date: '2026-10-24', path: 'days/2026/10/24.json', count: 2, first_observation_utc: '2026-10-24T10:00:00Z', last_observation_utc: '2026-10-24T23:59:59Z' },
      { date: '2026-10-25', path: 'days/2026/10/25.json', count: 1, first_observation_utc: '2026-10-25T01:00:00Z', last_observation_utc: '2026-10-25T01:00:00Z' }],
    scan_windows: [{ start_utc: '2026-10-24T00:00:00.000Z', end_utc: '2026-10-25T12:00:00.000Z', successful_runs: 5 }],
    archived_observations: 3, availability_note: 'Missing archives mean unknown, not no lightning', scan_window_note: 'not a guarantee'
  };
}
function fixtures(index = v2Index(), { feed = null, bodies = {} } = {}) {
  const body = new Map([
    [base, index],
    [new URL('days/2026/10/24.json', base).href, shard('2026-10-24', [flash('2026-10-24T10:00:00Z', 'first'), flash('2026-10-24T23:59:59Z', 'second')])],
    [new URL('days/2026/10/25.json', base).href, shard('2026-10-25', [flash('2026-10-25T01:00:00Z', 'third')])]
  ]);
  if (feed) body.set(feedUrl, feed);
  for (const [k, v] of Object.entries(bodies)) body.set(new URL(k, base).href, v);
  const requests = [];
  const fetchImpl = async (url, init = {}) => {
    requests.push({ url: url.replace(new URL('.', base).href, ''), cache: init.cache });
    if (!body.has(url)) return { ok: false, status: 404 };
    const b = body.get(url);
    if (b instanceof Error) throw b;
    if (typeof b === 'string') return { ok: true, status: 200, json: async () => JSON.parse(b) };
    return { ok: true, status: 200, json: async () => b };
  };
  return { body, requests, fetchImpl };
}
const client = (f, extra = {}) => createLightningHistoryClient({ indexUrl: base, feedUrl, fetchImpl: f.fetchImpl, now: () => NOW, ...extra });

// ------------------------------------------------------------------ windows
test('V6-like names, leap-year calendar and exact UTC window boundaries', () => {
  assert.equal(PERIODS['12m'], '1 year');
  const end = '2026-10-26T14:00:00Z';
  const year = selectUtcWindow({ period: '12m', end });
  assert.equal(year.start, '2025-10-26T14:00:00.000Z');
  assert.equal(year.end, '2026-10-26T14:00:00.000Z');
  assert.equal(year.bounds, '[start,end)');
  assert.equal(selectUtcWindow({ period: '12m', end: '2024-02-29T12:00:00Z' }).start, '2023-02-28T12:00:00.000Z');
  const dates = selectUtcDates('2026-10-24', '2026-10-25');
  assert.equal(dates.start, '2026-10-24T00:00:00.000Z');
  assert.equal(dates.end, '2026-10-26T00:00:00.000Z');
  assert.throws(() => selectUtcDates('2026-02-30', '2026-03-01'), RangeError);
});

test('Previous and next period navigation uses UTC and avoids future lookahead', () => {
  const initial = selectUtcWindow({ period: '7d', end: '2026-10-26T14:00:00Z' });
  const prev = previousUtcWindow(initial);
  assert.equal(prev.end, initial.start);
  assert.equal(nextUtcWindow(prev, '2026-10-26T14:00:00Z').end, initial.end);
  assert.strictEqual(nextUtcWindow(initial, '2026-10-26T14:00:00Z'), initial);
  assert.equal(canGoNext(initial, NOW), false);
  assert.equal(canGoNext(prev, NOW), true);
});

test('Window end is clamped to now, never a future window', () => {
  const w = selectUtcWindow({ period: '24h', end: '2026-10-27T10:00:00Z', now: NOW });
  assert.equal(w.end, new Date(NOW).toISOString());
  assert.equal(w.start, '2026-10-25T14:00:00.000Z');
  assert.equal(w.clampedEnd, true);
  const c = clampWindow(selectUtcWindow({ period: 'custom', start: '2026-10-26T00:00:00Z', end: '2026-10-28T00:00:00Z' }), { nowMs: NOW });
  assert.equal(c.clampedEnd, true);
  assert.equal(c.window.end, new Date(NOW).toISOString());
  assert.equal(c.window.start, '2026-10-26T00:00:00.000Z');
});

test('Retention edge clamps and flags instead of throwing; entirely-before is a state', () => {
  const cutoff = Date.parse('2025-10-26T00:00:00Z');
  const straddling = clampWindow(selectUtcWindow({ period: '7d', end: '2025-10-28T00:00:00Z' }), { cutoffMs: cutoff, nowMs: NOW });
  assert.equal(straddling.clampedStart, true);
  assert.equal(straddling.beforeArchive, false);
  assert.equal(straddling.window.start, '2025-10-26T00:00:00.000Z');
  const before = clampWindow(selectUtcWindow({ period: '7d', end: '2025-10-25T00:00:00Z' }), { cutoffMs: cutoff, nowMs: NOW });
  assert.equal(before.beforeArchive, true);
  const idx = normaliseIndex(v2Index());
  assert.equal(canGoPrevious(selectUtcWindow({ period: '12m', end: '2026-10-26T14:00:00Z' }), idx), true);
  assert.equal(canGoPrevious(selectUtcDates('2025-10-26', '2025-10-27'), idx), false);
});

// -------------------------------------------------------------------- index
test('Index v2 is accepted and validated; v1 is normalised to v2 rows with coverage from scan_windows', () => {
  const v2 = normaliseIndex(v2Index());
  assert.equal(v2.schemaVersion, 2);
  assert.equal(v2.days.length, 366);
  assert.equal(v2.dayMap.get('2026-10-24').coverage, 'full');
  const v1 = normaliseIndex(v1Index());
  assert.equal(v1.schemaVersion, 1);
  assert.equal(v1.days.length, 366);
  const d24 = v1.dayMap.get('2026-10-24');
  assert.deepEqual([d24.coverage, d24.scanned_seconds, d24.count, d24.path, d24.sealed], ['full', 86400, 2, 'days/2026/10/24.json', true]);
  const d25 = v1.dayMap.get('2026-10-25');
  assert.deepEqual([d25.coverage, d25.scanned_seconds, d25.count, d25.sealed], ['partial', 43200, 1, false]);
  const d23 = v1.dayMap.get('2026-10-23');
  assert.deepEqual([d23.coverage, d23.count, d23.path], ['none', null, null]);
  assert.equal(v1.lastScanEndMs, Date.parse('2026-10-25T12:00:00Z'));
  assert.throws(() => normaliseIndex({ ...v2Index(), schema: 'uk-lightning-history-index.v3' }), /Unsupported/);
  assert.throws(() => normaliseIndex({ ...v2Index(), observation: 'strikes' }), /Invalid historical lightning index/);
  const badCount = v2Index({ '2026-10-23': { coverage: 'none', scanned_seconds: 0, count: 0 } });
  assert.throws(() => normaliseIndex(badCount), /count must be null iff coverage none/);
  const zeroUnscanned = v2Index({ '2026-10-23': { coverage: 'partial', scanned_seconds: 0, count: 0 } });
  assert.throws(() => normaliseIndex(zeroUnscanned), /count 0 needs scanned time/);
});

test('Staleness uses generated_utc age or next_update_due_utc', () => {
  const idx = normaliseIndex(v2Index());
  assert.equal(isStale(idx, NOW), false);
  assert.equal(isStale(idx, Date.parse('2026-10-26T16:01:00Z')), true);   // past next_update_due_utc
  assert.equal(isStale(normaliseIndex(v2Index({}, { next_update_due_utc: '2026-10-27T00:00:00Z' })), Date.parse('2026-10-26T17:30:00Z')), true); // > 4 h
});

// ----------------------------------------------------------------- coverage
test('Coverage per day and per window: searched fraction, runs, not-yet-ingested tail, unknown days', () => {
  const idx = normaliseIndex(v2Index());
  assert.deepEqual([dayCoverage(idx, '2026-10-21').coverage, dayCoverage(idx, '2026-10-23').coverage, dayCoverage(idx, '2026-10-23').count], ['full', 'none', null]);
  assert.equal(dayCoverage(idx, '2024-01-01').known, false);
  const w = selectUtcDates('2026-10-24', '2026-10-25');
  const c = windowCoverage(idx, w);
  assert.equal(c.windowSeconds, 172800);
  assert.equal(c.searchedSeconds, 129600);
  assert.equal(c.searchedFraction, 0.75);
  assert.equal(c.runs, 5);
  assert.equal(c.scanStart, '2026-10-24T00:00:00.000Z');
  assert.equal(c.scanEnd, '2026-10-25T12:00:00.000Z');
  assert.equal(c.notYetIngestedSeconds, 0);
  assert.deepEqual(c.unknownDays, []);
  const latest = windowCoverage(idx, selectUtcWindow({ period: '24h', end: NOW }));
  assert.equal(latest.notYetIngestedSeconds, (NOW - Date.parse('2026-10-26T13:00:09Z')) / 1000);
  const never = windowCoverage(idx, selectUtcDates('2026-06-01', '2026-06-02'));
  assert.equal(never.searchedFraction, 0);
  assert.deepEqual(never.unknownDays, ['2026-06-01', '2026-06-02']);
  assert.equal(never.beforeArchiveSeconds, 172800);
  // Day rows are a floor when no scan window is recorded for the date.
  const floored = normaliseIndex(v2Index({ '2026-10-23': { coverage: 'partial', scanned_seconds: 1200, count: 0 } }));
  assert.equal(windowCoverage(floored, selectUtcDates('2026-10-23', '2026-10-23')).searchedSeconds, 1200);
});

// ------------------------------------------------------------------- budget
test('Budget is decided from index bytes and counts before any shard request', () => {
  const idx = normaliseIndex(v2Index());
  const rows = idx.days.filter((r) => r.count);
  const ok = windowBudget(rows, selectUtcDates('2026-10-24', '2026-10-25'));
  assert.deepEqual([ok.bytes, ok.count, ok.requests, ok.withinBudget], [1650, 3, 2, true]);
  const big = windowBudget([v2Row('2026-10-23', { coverage: 'full', scanned_seconds: 86400, count: 120000, bytes: 48000000, path: 'days/2026/10/23.json' })],
    selectUtcDates('2026-10-23', '2026-10-23'));
  assert.equal(big.withinBudget, false);
  assert.match(big.reason, /120,000 indexed flashes exceed 40,000/);
  const estimated = windowBudget([v2Row('2026-10-23', { coverage: 'full', scanned_seconds: 86400, count: 10, path: 'days/2026/10/23.json' })], selectUtcDates('2026-10-23', '2026-10-23'));
  assert.deepEqual([estimated.bytes, estimated.estimated], [5300, true]);
});

test('Hourly parts: only the parts overlapping the window are targeted', () => {
  const row = v2Row('2026-10-23', { coverage: 'full', scanned_seconds: 86400, count: 30, bytes: 3000, sealed: true, parts: [
    { hour: '03', path: 'days/2026/10/23/03.json', count: 10, bytes: 1000, sha256: sha('c') },
    { hour: '04', path: 'days/2026/10/23/04.json', count: 10, bytes: 1000, sha256: sha('d') },
    { hour: '17', path: 'days/2026/10/23/17.json', count: 10, bytes: 1000, sha256: sha('e') }] });
  normaliseIndex(v2Index({ '2026-10-23': row }));
  const targets = shardTargets(row, selectUtcWindow({ period: 'custom', start: '2026-10-23T04:30:00Z', end: '2026-10-23T18:00:00Z' }));
  assert.deepEqual(targets.map((t) => t.path), ['days/2026/10/23/04.json', 'days/2026/10/23/17.json']);
  assert.equal(shardTargets(v2Row('2026-10-23', { coverage: 'full', scanned_seconds: 86400, count: 0 }), selectUtcDates('2026-10-23', '2026-10-23')).length, 0);
});

// --------------------------------------------------------------- the client
test('Year lookback reads index only; no 365 day downloads on mobile', async () => {
  const f = fixtures();
  const c = client(f);
  const annual = await c.loadWindow(selectUtcWindow({ period: '12m', end: NOW }));
  assert.equal(annual.mode, 'overview');
  assert.equal(annual.archivedDayCount, 3);
  assert.equal(annual.unknownDayCount, 360);
  assert.equal(annual.dailyCounts.length, 366);
  assert.deepEqual(f.requests.map((r) => r.url), ['index.json']);
  assert.equal(f.requests[0].cache, 'no-cache');
  assert.equal(annual.status.state, STATES.PARTIAL);
  assert.match(formatHistoryLabel(annual, { nowMs: NOW }), /Historical gaps are unknown/);
  assert.match(formatHistoryLabel(annual, { nowMs: NOW }), /360 of 366 UTC dates have no collection run/);
});

test('Detail is the default for short windows: exact time filter, dedupe, sealed-aware cache mode, LRU reuse', async () => {
  const f = fixtures();
  const c = client(f);
  const window = selectUtcWindow({ period: 'custom', start: '2026-10-24T11:00:00Z', end: '2026-10-26T00:00:00Z' });
  const once = await c.loadWindow(window);
  assert.equal(once.mode, 'detail');
  assert.equal(once.totalMatchingObservations, 2);
  assert.equal(once.geojson.features[0].id, 'TEST-second');
  assert.equal(once.firstObservationUtc, '2026-10-24T23:59:59Z');
  assert.equal(once.lastObservationUtc, '2026-10-25T01:00:00Z');
  assert.equal(once.truncated, false);
  assert.equal(f.requests.length, 3);
  const byUrl = Object.fromEntries(f.requests.map((r) => [r.url, r.cache]));
  assert.equal(byUrl['days/2026/10/24.json'], 'default');   // sealed shard: ordinary HTTP caching
  assert.equal(byUrl['days/2026/10/25.json'], 'no-cache');  // unsealed shard: revalidate
  const again = await c.loadWindow(window);
  assert.equal(again.totalMatchingObservations, 2);
  assert.equal(f.requests.length, 3);
  assert.equal(c.cacheStats().shards, 2);
  assert.match(captionLines(once, { nowMs: NOW }).flashes, /2 archived in window · 2 drawn/);
});

test('Long range refuses bulk exact points, and short range can display cap honestly', async () => {
  const f = fixtures();
  const c = client(f, { maxMapPoints: 1 });
  const annual = await c.loadWindow(selectUtcWindow({ period: '12m', end: NOW }), { detail: true });
  assert.equal(annual.mode, 'overview');
  assert.match(annual.detailReason, /Choose at most/);
  assert.equal(f.requests.length, 1);
  const short = await c.loadWindow(selectUtcDates('2026-10-24', '2026-10-25'));
  assert.equal(short.mode, 'detail');
  assert.equal(short.totalMatchingObservations, 3);
  assert.equal(short.shownObservations, 1);
  assert.equal(short.truncated, true);
  assert.match(captionLines(short, { nowMs: NOW }).flashes, /cap 1: newest drawn, 2 older not drawn/);
});

test('Never-scanned is UNKNOWN; scanned with zero flashes is EMPTY-COVERED; never zero strikes', async () => {
  const f = fixtures();
  const c = client(f);
  const never = await c.loadWindow(selectUtcDates('2026-06-01', '2026-06-02'));
  assert.equal(never.totalMatchingObservations, 0);
  assert.equal(never.status.state, STATES.UNKNOWN);
  assert.equal(never.coverage.searchedFraction, 0);
  assert.match(never.availabilityNote, /unknown, not no lightning/);
  assert.equal(f.requests.length, 1);  // count null → nothing to fetch
  const covered = await c.loadWindow(selectUtcDates('2026-10-21', '2026-10-21'));
  assert.equal(covered.totalMatchingObservations, 0);
  assert.equal(covered.status.state, STATES.EMPTY_COVERED);
  assert.equal(covered.coverage.searchedFraction, 1);
  assert.match(captionLines(covered, { nowMs: NOW }).searched, /Searched EUMETSAT 100% of window/);
  assert.match(captionLines(never, { nowMs: NOW }).searched, /0% of window — no collection run covers these dates/);
  assert.doesNotMatch(JSON.stringify(captionLines(never, { nowMs: NOW })), /0 strikes/);
});

test('Archive network and invalid manifest failures are explicit, never fabricated', async () => {
  let calls = 0;
  const c = createLightningHistoryClient({ indexUrl: base, feedUrl, now: () => NOW, fetchImpl: async () => { calls++; return { ok: false, status: 503 }; } });
  await assert.rejects(() => c.loadWindow(selectUtcWindow({ period: '24h', end: NOW })), /HTTP 503/);
  await assert.rejects(() => c.loadWindow(selectUtcWindow({ period: '24h', end: NOW })), /HTTP 503/);
  assert.equal(calls, 4); // index + feed fallback, twice; nothing cached from failures
  const bad = fixtures({ ...v2Index(), days: 'nope' });
  await assert.rejects(() => client(bad, { feedUrl: null }).loadWindow(selectUtcDates('2026-10-24', '2026-10-24')), /Invalid historical lightning index/);
});

test('Requests older than the retained year are clamped and flagged; entirely before is BEFORE-ARCHIVE with zero shard requests', async () => {
  const f = fixtures();
  const c = client(f);
  const beyond = await c.loadWindow(selectUtcWindow({ period: '7d', end: '2025-10-25T00:00:00Z' }));
  assert.equal(beyond.beforeArchive, true);
  assert.equal(beyond.status.state, STATES.BEFORE_ARCHIVE);
  assert.deepEqual(f.requests.map((r) => r.url), ['index.json']);
  assert.match(captionLines(beyond, { nowMs: NOW }).warning, /Window predates the archive .* not zero lightning/);
  const straddle = await c.loadWindow(selectUtcWindow({ period: '7d', end: '2025-10-28T00:00:00Z' }));
  assert.equal(straddle.clamped.start, true);
  assert.equal(straddle.window.start, '2025-10-26T00:00:00.000Z');
  assert.equal(straddle.status.state, STATES.UNKNOWN);
});

test('allSettled: one missing day file is named, other data and index counts are still returned, state FAIL', async () => {
  const f = fixtures(v2Index({
    '2026-10-23': { coverage: 'full', scanned_seconds: 86400, count: 5, bytes: 2600, path: 'days/2026/10/23.json', sealed: true,
      first_observation_utc: '2026-10-23T05:00:00Z', last_observation_utc: '2026-10-23T06:00:00Z', sha256: sha('f') }
  }));
  const c = client(f);
  const r = await c.loadWindow(selectUtcDates('2026-10-23', '2026-10-25'));
  assert.equal(r.mode, 'detail');
  assert.equal(r.failures.length, 1);
  assert.deepEqual(r.failures[0], { date: '2026-10-23', path: 'days/2026/10/23.json', error: 'HTTP 404' });
  assert.equal(r.totalMatchingObservations, 3);
  assert.equal(r.archivedDayCount, 8);
  assert.equal(r.requestedDays, 3);
  assert.equal(r.status.state, STATES.FAIL);
  assert.equal(r.status.colour, '#ff4444');
  assert.match(captionLines(r, { nowMs: NOW }).warning, /1 of 3 day files failed \(2026-10-23\); points incomplete, counts from index/);
});

test('Shard count mismatch against the index is a named warning (mid-write race)', async () => {
  const f = fixtures(v2Index(), { bodies: { 'days/2026/10/24.json': shard('2026-10-24', [flash('2026-10-24T10:00:00Z', 'only')]) } });
  const r = await client(f).loadWindow(selectUtcDates('2026-10-24', '2026-10-24'));
  assert.deepEqual(r.warnings, [{ date: '2026-10-24', kind: 'count-mismatch', indexCount: 2, shardCount: 1 }]);
});

test('Budget gate: a storm month is summary-first; LOAD POINTS loads newest days up to the cap and says N of M', async () => {
  const overrides = {};
  const bodies = {};
  // 30 storm days ending today (a window past now would be clamped to now).
  for (let i = 29; i >= 0; i--) {
    const date = new Date(NOW - i * 86400000).toISOString().slice(0, 10);
    const path = 'days/' + date.replaceAll('-', '/') + '.json';
    overrides[date] = { coverage: 'full', scanned_seconds: 86400, count: 4000, bytes: 1_600_000, path, sealed: true,
      first_observation_utc: date + 'T01:00:00Z', last_observation_utc: date + 'T02:00:00Z', sha256: sha('9') };
    bodies[path] = shard(date, [flash(date + 'T01:00:00Z', 'a' + i), flash(date + 'T02:00:00Z', 'b' + i)]);
  }
  const f = fixtures(v2Index(overrides), { bodies });
  const c = client(f, { maxMapPoints: 10000 });
  const w = selectUtcDates('2026-09-27', '2026-10-26');
  const summary = await c.loadWindow(w);
  assert.equal(summary.mode, 'overview');
  assert.equal(summary.budget.count, 120000);
  assert.equal(summary.budget.bytes, 48_000_000);
  assert.equal(summary.budget.withinBudget, false);
  assert.equal(summary.canLoadPoints, true);
  assert.equal(summary.archivedDayCount, 120000);
  assert.equal(f.requests.length, 1, 'no shard may be fetched before consent');
  const points = await c.loadWindow(w, { consentPoints: true });
  assert.equal(points.mode, 'detail');
  assert.deepEqual(points.loadedDays, ['2026-10-26', '2026-10-25', '2026-10-24']); // 3 x 4000 ≥ 10,000 cap
  assert.equal(points.skippedDays.length, 27);
  assert.equal(points.truncated, true);
  assert.equal(points.totalMatchingObservations, 6); // genuine features in the small shards; "N of M" from the index
  assert.equal(points.archivedDayCount, 120000);
  assert.equal(f.requests.length, 4);
  assert.equal(captionLines(points, { nowMs: NOW }).flashes,
    'Flashes 120,000 archived in window (index) · 6 drawn from the newest 3 of 30 days (cap 10,000; 27 older days not loaded)');
});

test('LRU keeps at most 8 shards; evicted shards are re-fetched, cached ones are not', async () => {
  const overrides = {};
  const bodies = {};
  for (let d = 1; d <= 12; d++) {
    const dd = String(d).padStart(2, '0');
    overrides['2026-10-' + dd] = { coverage: 'full', scanned_seconds: 86400, count: 1, bytes: 550, path: `days/2026/10/${dd}.json`, sealed: true,
      first_observation_utc: `2026-10-${dd}T01:00:00Z`, last_observation_utc: `2026-10-${dd}T01:00:00Z`, sha256: sha('7') };
    bodies[`days/2026/10/${dd}.json`] = shard('2026-10-' + dd, [flash(`2026-10-${dd}T01:00:00Z`, 'd' + d)]);
  }
  const f = fixtures(v2Index(overrides), { bodies });
  const c = client(f);
  const r = await c.loadWindow(selectUtcDates('2026-10-01', '2026-10-12'));
  assert.equal(r.totalMatchingObservations, 12);
  assert.equal(c.cacheStats().shards, 8);
  assert.equal(c.cacheStats().max, 8);
  const before = f.requests.length;
  await c.loadWindow(selectUtcDates('2026-10-12', '2026-10-12'));           // the newest shard is still cached
  assert.equal(f.requests.length, before);
  await c.loadWindow(selectUtcDates('2026-10-01', '2026-10-01'));           // evicted: fetched again
  assert.equal(f.requests.length, before + 1);
  assert.equal(c.cacheStats().shards, 8);
});

test('Index refresh invalidates only unsealed or changed shards', async () => {
  const f = fixtures();
  const c = client(f);
  await c.loadWindow(selectUtcDates('2026-10-24', '2026-10-25'));
  assert.equal(c.cacheStats().shards, 2);
  f.body.set(base, { ...v2Index(), generated_utc: '2026-10-26T14:00:00.000Z' });
  await c.loadWindow(selectUtcDates('2026-10-24', '2026-10-25'), { refreshIndex: true });
  const urls = f.requests.map((r) => r.url);
  assert.equal(urls.filter((u) => u === 'days/2026/10/24.json').length, 1, 'sealed, unchanged sha: kept');
  assert.equal(urls.filter((u) => u === 'days/2026/10/25.json').length, 2, 'unsealed: re-fetched');
});

test('Tier A: the live feed serves 12h-7d windows ending now with one request; the cap gap is completed from shards', async () => {
  const feed = { type: 'FeatureCollection', metadata: { total_before_display_cap: 3, query_start_utc: '2026-10-26T08:00:00Z', query_end_utc: '2026-10-26T13:05:58Z', scan_end_utc: '2026-10-26T13:00:09Z' },
    features: [flash('2026-10-25T01:00:00Z', 'third'), flash('2026-10-26T12:00:00Z', 'fresh')] };
  const f = fixtures(v2Index(), { feed });
  const c = client(f);
  const r = await c.loadWindow(selectUtcWindow({ period: '48h', end: NOW }), { tier: 'feed' });
  assert.equal(r.mode, 'feed');
  assert.equal(r.feedTruncated, true);
  assert.equal(r.totalMatchingObservations, 3); // 'second' (24 Oct 23:59) came from the shard the feed cap had dropped
  // The cap gap [window start, first feed flash) overlaps 24 Oct and the first hour of 25 Oct: both shards are completed.
  assert.deepEqual(f.requests.map((x) => x.url).sort(), ['days/2026/10/24.json', 'days/2026/10/25.json', 'index.json', feedUrl].sort());
  assert.equal(r.status.state, STATES.PARTIAL);
  await assert.rejects(() => c.loadWindow(selectUtcWindow({ period: '30d', end: NOW }), { tier: 'feed' }), /live feed covers only/);
});

test('Fallback: index 404 but feed available → feed result flagged, never a zero; both down → both named', async () => {
  const feed = { type: 'FeatureCollection', metadata: { total_before_display_cap: 1, query_start_utc: '2026-10-26T08:00:00Z', query_end_utc: '2026-10-26T13:05:58Z' },
    features: [flash('2026-10-26T12:00:00Z', 'fresh')] };
  const f = fixtures(v2Index(), { feed });
  f.body.delete(base);
  const r = await client(f).loadWindow(selectUtcWindow({ period: '24h', end: NOW }));
  assert.equal(r.mode, 'feed');
  assert.equal(r.fallback, 'feed');
  assert.equal(r.indexError, 'HTTP 404');
  assert.equal(r.totalMatchingObservations, 1);
  assert.equal(r.coverage.fromFeedMetadataOnly, true);
  assert.equal(r.status.state, STATES.PARTIAL);
  assert.match(r.status.reasons.join(' '), /archive unavailable/);
  assert.match(captionLines(r, { nowMs: NOW }).refresh, /archive not yet published: HTTP 404/);
  await assert.rejects(() => client(f).loadWindow(selectUtcWindow({ period: '30d', end: NOW })), /History index unavailable: HTTP 404/);
  f.body.delete(feedUrl);
  await assert.rejects(() => client(f).loadWindow(selectUtcWindow({ period: '24h', end: NOW })), /index unavailable: HTTP 404; live feed unavailable: HTTP 404/);
});

test('The nine UX states exist with their colours, and the data states are reachable', async () => {
  assert.deepEqual(Object.values(STATES), ['OFF', 'LOADING', 'READY·DETAIL', 'READY·SUMMARY', 'EMPTY-COVERED', 'PARTIAL', 'UNKNOWN', 'BEFORE-ARCHIVE', 'FAIL']);
  assert.equal(Object.keys(STATE_COLOURS).length, 9);
  assert.equal(STATE_COLOURS[STATES.PARTIAL], '#ffcc00');
  const f = fixtures(v2Index({ '2026-10-24': { coverage: 'full', scanned_seconds: 86400, count: 2, bytes: 1100, path: 'days/2026/10/24.json', sealed: true,
    first_observation_utc: '2026-10-24T10:00:00Z', last_observation_utc: '2026-10-24T23:59:59Z', sha256: sha('a') } }));
  const c = client(f);
  const ready = await c.loadWindow(selectUtcDates('2026-10-24', '2026-10-24'));
  assert.equal(ready.status.state, STATES.READY_DETAIL);
  assert.equal(ready.status.stale, false);
  const summary = await c.loadWindow(selectUtcDates('2026-10-24', '2026-10-24'), { detail: false });
  assert.equal(summary.status.state, STATES.READY_SUMMARY);
  const partial = await c.loadWindow(selectUtcDates('2026-10-25', '2026-10-25'));
  assert.equal(partial.status.state, STATES.PARTIAL);
  assert.deepEqual(partial.status.reasons, ['gaps inside the window']);
  const stale = classifyState(ready, { nowMs: Date.parse('2026-10-26T18:00:00Z') });
  assert.equal(stale.stale, true);
  assert.match(captionLines(ready, { nowMs: Date.parse('2026-10-26T18:00:00Z') }).refresh, /STALE/);
});

test('Caption templates carry window, actual, flashes, refresh, searched, warning and the optical-flash footer', async () => {
  const f = fixtures();
  const r = await client(f).loadWindow(selectUtcWindow({ period: '24h', end: NOW }));
  const l = captionLines(r, { nowMs: NOW });
  assert.equal(l.window, 'Window  2026-10-25 14:00 UTC → 2026-10-26 14:00 UTC (24 hours, latest)');
  assert.match(l.actual, /^Actual  no archived flashes within this window · last archived flash before window 2026-10-25 01:00:00 UTC \(13h 0min before start\)$/);
  assert.equal(l.flashes, 'Flashes 0 archived in window · 0 drawn');
  assert.equal(l.refresh, 'Refresh archive 2026-10-26 13:00 UTC (1h 0min ago; expected every 1 h)');
  assert.match(l.searched, /^Searched EUMETSAT \d+% of window · 1 successful runs · 2026-10-26 10:05 UTC → 2026-10-26 13:00 UTC$/);
  assert.match(l.warning, /Not yet ingested: last 1h 0min of window \(unknown, not zero\)/);
  assert.match(l.footer, /not confirmed ground strikes · Missing days are unknown, not zero · UK box/);
  const annual = await client(fixtures()).loadWindow(selectUtcWindow({ period: '12m', end: NOW }));
  const a = captionLines(annual, { nowMs: NOW });
  assert.match(a.actual, /on the 366 overlapping UTC dates: first 2026-10-24 10:00:00 UTC · last 2026-10-25 01:00:00 UTC/);
  assert.match(a.flashes, /3 archived on 366 overlapping UTC dates · none drawn \(summary mode/);
  assert.match(a.warning, /precede archive start 2026-10-20 \(not zero\)/);
});
