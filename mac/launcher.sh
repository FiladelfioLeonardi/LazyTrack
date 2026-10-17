#!/bin/bash
# The LazyTrack app in ~/Applications: opens LazyTrack's window, starting LazyTrack first if needed.
# "--background" (used at login) starts it without opening the browser.
APP_DIR="$HOME/Library/Application Support/LazyTrack"
if curl -s -m 2 -o /dev/null http://127.0.0.1:8042/api/auth/state; then
  # already running: show its window (or the browser if the window isn't available)
  if [ "$1" != "--background" ]; then
    cd "$APP_DIR" && nohup "$APP_DIR/.venv/bin/python" -m lazytrack window >>"$HOME/.lazytrack/window.log" 2>&1 &
  fi
  exit 0
fi
[ "$1" = "--background" ] && export LAZYTRACK_RESTARTED=1
nohup "$APP_DIR/mac/run.sh" >/dev/null 2>&1 &
