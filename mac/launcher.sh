#!/bin/bash
# The LazyTrack app in ~/Applications: opens LazyTrack, starting it first if needed.
# "--background" (used at login) starts it without opening the browser.
APP_DIR="$HOME/Library/Application Support/LazyTrack"
if curl -s -m 2 -o /dev/null http://127.0.0.1:8042/api/auth/state; then
  [ "$1" = "--background" ] || open "http://localhost:8042"
  exit 0
fi
[ "$1" = "--background" ] && export LAZYTRACK_RESTARTED=1
nohup "$APP_DIR/mac/run.sh" >/dev/null 2>&1 &
