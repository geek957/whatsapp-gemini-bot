#!/usr/bin/env bash
# Run the bot now instead of waiting for the schedule.
#
# GitHub's cron is best-effort and can skip for hours, so this is the reliable way to get a
# reply immediately. Reuses the credential git already stored for this repository, so it
# needs no extra setup and no token on the command line.
#
# Usage:
#   scripts/trigger.sh              # dispatch the workflow and wait for the result
#   scripts/trigger.sh --no-wait    # dispatch and return straight away
#   scripts/trigger.sh --doctor     # run connectivity checks instead of processing
#   scripts/trigger.sh --dry-run    # match messages without calling Gemini or replying
set -euo pipefail

cd "$(dirname "$0")/.."

# Parameter expansion rather than sed: BSD sed has no lazy quantifiers, so an ERE that works
# on GNU fails on macOS.
remote_url="$(git remote get-url origin)"
repo="${remote_url#*github.com/}" # https form
repo="${repo#*github.com:}"       # ssh form
repo="${repo%.git}"
repo="${repo%/}"
token="$(sed -n 's|https://[^:]*:\(.*\)@github.com|\1|p' .git/.git-credentials 2>/dev/null | head -1)"
if [ -z "${token}" ]; then
  echo "No stored GitHub credential. Run: git push   (it will prompt and store one)" >&2
  exit 1
fi

wait_for_result=1
inputs="{}"
for arg in "$@"; do
  case "$arg" in
  --no-wait) wait_for_result=0 ;;
  --doctor) inputs='{"doctor":true}' ;;
  --dry-run) inputs='{"dry_run":true}' ;;
  *)
    echo "unknown option: $arg" >&2
    exit 2
    ;;
  esac
done

api() { curl -sS -H "Authorization: Bearer ${token}" -H "Accept: application/vnd.github+json" "$@"; }

echo "dispatching whatsapp-gemini on ${repo}"
code="$(api -o /dev/null -w '%{http_code}' -X POST \
  "https://api.github.com/repos/${repo}/actions/workflows/whatsapp-gemini.yml/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":${inputs}}")"
if [ "$code" != "204" ]; then
  echo "dispatch failed (HTTP ${code})" >&2
  exit 1
fi

if [ "$wait_for_result" = "0" ]; then
  echo "dispatched: https://github.com/${repo}/actions"
  exit 0
fi

sleep 12
run_id=""
for _ in 1 2 3 4 5; do
  run_id="$(api "https://api.github.com/repos/${repo}/actions/workflows/whatsapp-gemini.yml/runs?event=workflow_dispatch&per_page=1" |
    python3 -c 'import json,sys; runs=json.load(sys.stdin)["workflow_runs"]; print(runs[0]["id"] if runs else "")')"
  [ -n "$run_id" ] && break
  sleep 5
done
[ -n "$run_id" ] || { echo "dispatched, but could not find the run; check the Actions tab" >&2; exit 0; }

echo "run ${run_id}: https://github.com/${repo}/actions/runs/${run_id}"
printf 'waiting'
for _ in $(seq 1 40); do
  status="$(api "https://api.github.com/repos/${repo}/actions/runs/${run_id}" |
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["conclusion"] or "")')"
  case "$status" in
  completed*)
    echo " -> ${status}"
    break
    ;;
  esac
  printf '.'
  sleep 15
done

# The run prints a JSON report; pull the useful counters out of its log.
tmp="$(mktemp -d)"
if api -L -o "${tmp}/logs.zip" "https://api.github.com/repos/${repo}/actions/runs/${run_id}/logs" 2>/dev/null &&
  unzip -qo "${tmp}/logs.zip" -d "${tmp}/logs" 2>/dev/null; then
  grep -rhE '"(read|images_seen|already_processed|not_triggered|replies|status|detail)"' "${tmp}/logs" |
    sed 's/^[0-9TZ:.-]*Z\? //' | sort -u
fi
rm -rf "$tmp"
