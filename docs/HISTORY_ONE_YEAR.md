# Proposed UK Lightning: truthful time labels and 12-month history

**Status: independent data-plane prototype, NOT a live GridAtlas implementation. The owner rejected the earlier V1–V5 preview as visually unlike the original. No automatic merge or deployment.**

## Source implementation: GlobalGrid2050 UK Energy Tracker V6

Reference: https://globalgrid2050.com/uk_energy_tracking_v6/

The actual code is in Ventusltd/globalgrid2050:

- uk_energy_tracking_v6/price_history_chart/control_price_history/control_price_history.js: year/start/period controls, previous/next period, fullscreen synchronisation, selected-range labels and debouncing.
- uk_energy_tracking_v6/price_history_chart/load_price_history_data/load_price_history_data.js: periodDays, selectedWindow, cached per-year CSV requests, UTC filtering, high-resolution vs daily tiers.
- uk_energy_tracking_v6/generation_history/load_generation_history_data.js: only recent 30-day high-frequency loading; daily summaries for long periods.
- uk_energy_tracking_v6/generation_history/generation_daily_mwh_by_technology_fuelhh_index.json: historical chunk index pattern, explicitly naming years, path, rows and bytes.

Reuse **date logic, lazy tiers and storage design — NOT the Tracker's HTML/CSS or a substitute GridAtlas UI.**

## Required lightning labels

The previous label showed only flash count. It must show, separately:
1. **Selected UTC window:** exact start and end, with end-exclusive precision.
2. **Actual earliest/latest archived flashes** within the loaded window, not fictional coverage.
3. **Number of genuine detections** and number of points rendered (if capped).
4. **Last ingestion time** and, separately, dates requested from the provider.
5. **Historical status:** complete archive NOT guaranteed; missing day means unknown, not zero lightning.
6. **Optical satellite flashes, NOT verified ground strikes.**

Example label text is *illustrative only*: "Selected 19–26 Oct 2026 UTC | Actual detected 22–25 Oct | 408 optical flashes | Updated 26 Oct 13:05 UTC | Partial historical archive | Not verified cloud-to-ground strikes". Every number/date must come from the actual loaded files.

## One-year storage and browsing

The Python history.py prototype writes validated EUMETSAT points, uncapped for archival purposes, grouped by **UTC day**:

    data/history/index.json
    data/history/days/2026/10/22.json
    data/history/days/2026/10/23.json
    ...

Each day file is a normal **GeoJSON FeatureCollection** saved as a .json document. The original GridAtlas Parquet bridge has a global .geojson interception problem. Dated .json filenames avoid triggering that interceptor and allow native fetch to remain untouched; prove this in served-browser tests. The index lists only days that have archived genuine detections, counts, min/max detection times and successful collection search windows. Search windows are evidence of requested source periods, *not guaranteed source completeness*.

The archive retains **one calendar year** of UTC dates, deduplicates by stable observation ID, refuses malformed existing files, and prunes older days. Its first run can seed the previous **seven-day rolling feed**. That is NOT a historical year of data. Genuine EUMETSAT historical products would require a separate bounded, verified backfill.

### Browser time-window client (no UI changes)

The standalone history-window.mjs exposes **12h, 24h, 48h, 7d, 30d, 3m, 6m, 12m and custom UTC** selection with previous/next period navigation, a human-readable status label and resource caching. It has **no DOM, CSS or global fetch rewriting**.

- Short intervals (up to 31 days): lazy-load only matching day shards; at most four concurrent requests; filter exact timestamps; deduplicate; respect display cap with explicit displayed/total counters.
- Long intervals (three months through twelve months): load **only the index** and present archived daily counts. Do not download or render an entire year of individual flash points into a mobile map. Select a historic week/month for exact event coordinates.
- A missing day/empty archive is marked **UNKNOWN**, never "0 strikes". Archived flash count is not equivalent to a ground strike tally.
- The UI implant may borrow these date and range controls, but must be built into the **original GridAtlas look** by a separate Claude-reviewed release.

### Storage limits and operation

A GitHub repository can accumulate considerable data during busy storm periods. Monitor day-shard bytes, index size, Git commit growth and mobile bandwidth. If daily files exceed practical Git or raw-serving budgets, move sealed shards to dedicated data/static object storage while keeping an indexed, hashed contract. Do not create one enormous 12-month GeoJSON. The proposed workflow updates the archive on the existing three-hour collection schedule only **after this PR is approved and merged**.

### Owner preservation gate

See https://github.com/Ventusltd/gridatlas/issues/28

- Production Atlas remains generation 202609241334.
- No new full application copy, no rewriting the old map engine, CSS, menu or global fetch bridge.
- Multi-agent Claude read-only architecture, transport, storage, UX and independent veto reviews precede any live UI changes.
- Require pixel/layout comparison desktop and Android/iPhone-sized browser tests **against the original**, original 60+3 menu contract, full control/SLD tests and real-device owner screenshots.
- Data archive branch and UI integration branch must remain independent until both pass.
