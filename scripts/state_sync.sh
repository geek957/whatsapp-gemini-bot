#!/usr/bin/env bash
# Read and write the dedupe state on a dedicated orphan branch.
#
# Git plumbing is used instead of a checkout so the working tree is never switched: the
# workflow keeps the code from the default branch while committing a single-file tree to
# the state branch. Nothing else lives on that branch.
#
# Usage:
#   scripts/state_sync.sh pull <branch> <local-path>
#   scripts/state_sync.sh push <branch> <local-path> <message>
set -euo pipefail

action="${1:?usage: state_sync.sh pull|push <branch> <path> [message]}"
branch="${2:?missing branch}"
path="${3:?missing path}"
message="${4:-state: update dedupe records}"
state_file="state.json"

fetch_remote() {
  git fetch --quiet --depth=1 origin "refs/heads/${branch}:refs/remotes/origin/${branch}" 2>/dev/null || return 1
}

case "$action" in
pull)
  mkdir -p "$(dirname "$path")"
  if fetch_remote && git cat-file -e "origin/${branch}:${state_file}" 2>/dev/null; then
    git show "origin/${branch}:${state_file}" >"$path"
    echo "state: pulled $(wc -c <"$path" | tr -d ' ') bytes from ${branch}"
  else
    echo '{"version":1,"processed":{}}' >"$path"
    echo "state: no ${branch} branch yet, starting empty"
  fi
  ;;

push)
  [ -f "$path" ] || { echo "state: nothing at $path to push" >&2; exit 1; }

  for attempt in 1 2 3; do
    parent=""
    if fetch_remote; then
      parent="$(git rev-parse --verify --quiet "origin/${branch}^{commit}" || true)"
      # Another run may have pushed while this one was working; union the two files.
      if [ -n "$parent" ] && git cat-file -e "origin/${branch}:${state_file}" 2>/dev/null; then
        git show "origin/${branch}:${state_file}" >"${path}.remote"
        python3 -m bot.cli state-merge --ours "$path" --theirs "${path}.remote" --out "$path"
        rm -f "${path}.remote"
      fi
    fi

    blob="$(git hash-object -w "$path")"
    tree="$(printf '100644 blob %s\t%s\n' "$blob" "$state_file" | git mktree)"
    if [ -n "$parent" ]; then
      commit="$(git commit-tree "$tree" -p "$parent" -m "$message")"
    else
      commit="$(git commit-tree "$tree" -m "$message")"
    fi

    if git push --quiet origin "${commit}:refs/heads/${branch}"; then
      echo "state: pushed ${commit:0:8} to ${branch}"
      exit 0
    fi
    echo "state: push rejected, retrying ($attempt/3)" >&2
    sleep $((attempt * 2))
  done

  echo "state: could not push after 3 attempts" >&2
  exit 1
  ;;

*)
  echo "unknown action: $action" >&2
  exit 2
  ;;
esac
