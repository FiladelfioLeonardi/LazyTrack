#!/bin/bash
# Runs LazyTrack in the background (started by the LazyTrack app). Restarts it when it asks to.
cd "$(dirname "$0")/.."
mkdir -p "$HOME/.lazytrack"
while true; do
  "$PWD/.venv/bin/python" -m lazytrack run >> "$HOME/.lazytrack/lazytrack.log" 2>&1
  [ $? -eq 3 ] || break
  export LAZYTRACK_RESTARTED=1
done
