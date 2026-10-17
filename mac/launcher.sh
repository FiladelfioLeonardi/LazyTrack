#!/bin/bash
# The LazyTrack app in ~/Applications: opens LazyTrack's window, starting LazyTrack first if needed.
# "--background" (used at login and by the installer) starts it without a window.
#
# The window runs as this app's own process (exec), so macOS knows the app is open exactly while the
# window is: closing the window quits the app, and opening the app again always brings a window.
# The tracker itself runs on in the background (run.sh), unless "keep running" is off in Settings.
APP_DIR="$HOME/Library/Application Support/LazyTrack"
mkdir -p "$HOME/.lazytrack"
if ! curl -s -m 2 -o /dev/null http://127.0.0.1:8042/api/auth/state; then
  # not running yet: start the tracker in the background (it doesn't open a window itself)
  export LAZYTRACK_RESTARTED=1
  nohup "$APP_DIR/mac/run.sh" >/dev/null 2>&1 &
  disown
fi
[ "$1" = "--background" ] && exit 0
cd "$APP_DIR" && exec "$APP_DIR/.venv/bin/python" -m lazytrack window >>"$HOME/.lazytrack/window.log" 2>&1
