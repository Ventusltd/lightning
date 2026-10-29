# Operations: keeping the lightning archive current

This page lists what the hourly collector depends on, what alerts when each
dependency fails, and what to do about it.

## Single points of failure and their alarms

| dependency | what fails | alarm |
| --- | --- | --- |
| GitHub cron (`17,47 * * * *` in `update-lightning.yml`) | GitHub drops most scheduled runs. On 28 Oct 2026 only 10 of the 48 slots ran. | `collector-stale` issue |
| The Claude "hourly nudge" scheduled task (`29 * * * *`, dispatches `update-lightning.yml`) | It became the de facto primary scheduler (it dispatched 17 of the 27 runs on 28 Oct). It can stop silently: a task that is disabled, a session or account that expires, or approvals that are no longer automatic. | `collector-stale` issue |
| EUMETSAT credentials (`EUMETSAT_CONSUMER_KEY`, `EUMETSAT_CONSUMER_SECRET`) | The key belongs to one EUMETSAT account. When it is revoked, expires or is rate-limited, every run fails. | `collector-stale` issue (state `failing`) |
| `raw.githubusercontent.com/Ventusltd/lightning/main/data/...` | This is the path the public map reads. It has no SLA and has been rate-limited since 2025. | `feed-delivery` issue |

## The `collector-stale` issue

`ci/collector_health.py` reads the heartbeat that `status.py write` puts in
`data/status.json` after every run, successful or not. It reaches one of four
verdicts:

| verdict | condition | issue |
| --- | --- | --- |
| `ok` | The heartbeat is on time and the runs succeed. | An open issue is closed, with a recovery comment. |
| `late` | Now is past `heartbeat.late_after_utc` (the last attempt plus 90 min), but within the grace period. | No issue. A warning appears in the run summary. |
| `stale` | Now is past `late_after_utc` plus the grace period. The default grace is 90 min, so this means no heartbeat for 3 h. A missing or unreadable `status.json` also counts. | One issue is opened, or refreshed if already open. |
| `failing` | Runs happen but fail: `consecutive_failures` is 3 or more, or `freshness` is `stale`, `stopped` or `never`. | One issue is opened, or refreshed if already open. |

There is only ever one open issue with the label. When it is already open, its
body is replaced on each check, which sends no notification. A comment is added
only when the state changes, for example from `failing` to `stale`. The first
successful run after the fault closes the issue.

**Why the 90 min grace.** In the 53 heartbeats from 27 to 29 Oct 2026, a gap
past `late_after_utc` happened 7 times, 95 to 281 minutes each. That is roughly
three a day, and nearly all were one or two dropped cron slots that the next
run healed. An issue at `late_after_utc` itself would open and close about three
times a day, and people learn to ignore an alarm like that. Gaps of 3 h or more
happened only on 27 Oct, before the nudge existed. Each of them was a real
outage that someone should have looked at. To alert exactly at `late_after_utc`,
pass `--grace-minutes 0`.

### Where the check runs

1. **At the end of every hourly run** (`update-lightning.yml`, step
   "Collector health issue"). It runs after the status commit and before the
   step that turns the run red. It uses `continue-on-error`, so monitoring never
   changes the collector's own result. This step closes the issue when the
   collector recovers, and opens it when failures accumulate.
2. **In `verify-lightning-data.yml`**, as a separate `health` job. That job is
   the only one in the file with `issues: write`; the data checks stay
   read-only. The file is started by every hourly run and every backfill
   completion, so a backfill also reports an hourly collector that has stopped.
3. **In the watchdog** (`collector-watchdog.yml`), explained below.

### Watchdog

Checks 1 and 2 run only after a writer workflow has run. When the cron and the
nudge both stop, no writer runs, so nothing is left to notice that no run
happened. A third, independent schedule is the minimum that can detect it.
`collector-watchdog.yml` is kept deliberately small:

- It runs at `8,38 * * * *`. These slots avoid :17/:47 (the collector) and
  :29 (the nudge). There are two slots an hour because GitHub drops scheduled
  runs.
- It does a sparse checkout of two files, runs the Python standard library
  already on the runner, and takes about 15 s.
- It has `contents: read`. It never commits, never collects and never
  dispatches another workflow.
- It also probes the `raw.githubusercontent.com` path (see `feed-delivery`
  below).
- It exits red while anything is unhealthy. GitHub e-mails a failed scheduled
  run to the person who last edited the cron, which gives a second alarm.

The watchdog is a GitHub schedule too, so a GitHub-wide scheduler outage
delays it as well. The other alarms are independent of it: the nudge's own
runs, which pass through check 1, and any backfill, which passes through
check 2. GitHub also disables scheduled workflows in a repository with no
activity for 60 days. The hourly data commits keep this repository active.

## What to do

### `collector-stale`, state `stale` (no heartbeat)

1. Open **Actions → UK lightning optical flashes**. Check whether any run was
   created after `last_attempt_utc`.
2. **Start a run yourself:** go to **Run workflow** and leave `hours` empty.
   The self-healing lookback (up to 48 h) covers the gap. For gaps longer than
   48 h, run **UK lightning archive backfill** for the missing dates.
3. Check the Claude "hourly nudge" scheduled task. It should be enabled, set
   to `29 * * * *`, and approve automatically. Re-enable it or create it
   again.
4. Check that the workflow has not been disabled under **Actions → (workflow)
   → … → Enable workflow**.
5. The next successful run closes the issue. Flashes in the gap are unknown,
   not zero.

### `collector-stale`, state `failing` (runs fail)

1. Read `last_error` in the issue, or in `data/status.json`.
2. **Credentials** (HTTP 401/403, `invalid_client`): sign in to the EUMETSAT
   account that owns the key at <https://api.eumetsat.int/api-key/>. Generate a
   new consumer key and secret. Update both repository secrets under
   **Settings → Secrets and variables → Actions**:
   `EUMETSAT_CONSUMER_KEY` and `EUMETSAT_CONSUMER_SECRET`. Then dispatch a run.
   Record who owns the account and where its recovery details are kept.
   Today a single person holds it.
3. **EUMETSAT outage or rate limit** (HTTP 5xx, 429, timeouts): see
   `last_download_retries` in `status.json`. Wait, or dispatch a run later.
   The lookback catches up for up to 48 h.
4. **Code or dependency failure** (a Python traceback): fix it with a PR.
   `test-lightning.yml` runs on it.

### `feed-delivery` (raw.githubusercontent.com)

The committed data may be fine while the public path fails. The issue lists
which file returned which HTTP status, or how far the served `status.json` is
behind main.

- An HTTP 429, or a short run of 5xx errors, is GitHub's rate limiting or an
  incident. It usually clears by itself, and the next passing probe closes the
  issue.
- When it lasts for hours, the lasting fix is to serve the map's files from
  GitHub Pages or a pinned commit URL instead of `.../main/`. That is a change
  to the map, not to this repository. See GridAtlas reports/V-maintain.md,
  recommendation P2.

## Running the check by hand

```sh
python ci/collector_health.py --status data/status.json                 # verdict only
python ci/collector_health.py --status data/status.json --raw           # + raw probe
GH_TOKEN=... python ci/collector_health.py --status data/status.json --issue --repo Ventusltd/lightning
python ci/collector_health.py --status data/status.json --now 2026-10-29T12:00:00Z --grace-minutes 0
```

`python -m unittest tests.test_collector_health` covers the verdicts, the
issue sync (open, refresh, comment on a state change, close, duplicates) and
the raw probe, all against fakes with no network access. It also checks how
the three workflows are wired.
