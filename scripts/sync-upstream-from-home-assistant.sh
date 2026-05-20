#!/usr/bin/env bash
# Sync a home-assistant/* repository branch to local upstream and push to origin.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
: "${UPSTREAM_URL:?Set UPSTREAM_URL (e.g. https://github.com/home-assistant/actions.git)}"
TRACK_BRANCH="${TRACK_BRANCH:?Set TRACK_BRANCH (e.g. master, main, or dev)}"
LOCAL_BRANCH="${LOCAL_BRANCH:-upstream}"

if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
	git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
fi

git -c fetch.recurseSubmodules=false fetch "$UPSTREAM_REMOTE" "$TRACK_BRANCH"
git checkout -B "$LOCAL_BRANCH" "${UPSTREAM_REMOTE}/${TRACK_BRANCH}"

if [ -f .gitmodules ]; then
	git submodule sync --recursive
	git submodule update --init --recursive
fi

git push --force-with-lease origin "$LOCAL_BRANCH"
