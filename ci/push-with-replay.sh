#!/usr/bin/env bash
# Commit PATHs and push to origin/<current branch>, surviving a
# concurrent writer without a textual merge of generated files.
#
#   ci/push-with-replay.sh MESSAGE REPLAY_COMMAND PATH...
#
# The hourly collector and the backfill run in separate concurrency groups
# (so a long backfill can never hold the hourly feed back) and both write
# data/history/index.json and data/status.json. A `git pull --rebase` of two
# regenerated index.json files conflicts, so on a rejected push this script
# instead resets to the new origin/main and runs REPLAY_COMMAND, which
# regenerates the outputs on top of the other writer's commit from inputs
# kept outside the repository ($RUNNER_TEMP run records). Then it commits and
# pushes again, up to $PUSH_ATTEMPTS times (default 5).
#
# Optional: PUSH_BODY (second -m paragraph), PUSH_RETRY_SLEEP (seconds, default
# 5; multiplied by the attempt number), PUSH_VERIFY (a command run after
# staging and before EVERY commit, the first and each replayed one; if it fails
# nothing is committed or pushed - the data workflows set it to
# `python ci/verify_data.py --staged`).
set -euo pipefail
if [ "$#" -lt 3 ]; then
  echo "usage: $0 MESSAGE REPLAY_COMMAND PATH..." >&2
  exit 2
fi
message=$1
replay=$2
shift 2
# The branch the workflow checked out (main for the schedule), never a fixed
# name: a dispatch on another branch must not push to main.
branch=${PUSH_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}
if [ "$branch" = "HEAD" ]; then
  echo "::error::push-with-replay: detached HEAD; set PUSH_BRANCH" >&2
  exit 2
fi
attempts=${PUSH_ATTEMPTS:-5}
pause=${PUSH_RETRY_SLEEP:-5}

stage() {
  local p
  for p in "$@"; do
    # A path that does not exist yet (data/history before the first archive
    # write) is skipped: a combined pathspec would abort with 128.
    if [ -e "$p" ] || [ -n "$(git ls-files -- "$p")" ]; then
      git add -A -- "$p"
    fi
  done
}

attempt=1
while :; do
  stage "$@"
  if git diff --cached --quiet; then
    echo "push-with-replay: nothing to commit"
    exit 0
  fi
  if [ -n "${PUSH_VERIFY:-}" ] && ! bash -e -c "$PUSH_VERIFY"; then
    echo "::error::push-with-replay: PUSH_VERIFY failed on attempt $attempt; nothing was committed or pushed"
    git reset -q
    exit 1
  fi
  if [ -n "${PUSH_BODY:-}" ]; then
    git commit -q -m "$message" -m "$PUSH_BODY"
  else
    git commit -q -m "$message"
  fi
  if git push -q origin "HEAD:$branch"; then
    echo "push-with-replay: pushed on attempt $attempt"
    exit 0
  fi
  if [ "$attempt" -ge "$attempts" ]; then
    echo "::error::push-with-replay: push rejected $attempt times; nothing was committed"
    exit 1
  fi
  # The replay resets the working tree to the new main. Every output of this
  # step is in PATHs (just committed); refuse rather than silently discard any
  # other uncommitted change.
  others=$(git diff --name-only HEAD)
  if [ -n "$others" ]; then
    echo "::error::push-with-replay: refusing to reset over uncommitted changes outside the step's paths: $others"
    exit 1
  fi
  echo "::warning::push rejected (attempt $attempt/$attempts): another writer moved origin/$branch; replaying onto it"
  sleep $(( pause * attempt ))
  git fetch -q origin "$branch"
  git reset -q --hard "origin/$branch"
  for p in "$@"; do
    if [ -e "$p" ]; then git clean -fdq -- "$p"; fi
  done
  bash -e -c "$replay"
  attempt=$(( attempt + 1 ))
done
