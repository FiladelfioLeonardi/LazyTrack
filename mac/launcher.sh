#!/bin/bash
# The LazyTrack app in ~/Applications: opens LazyTrack's window, starting LazyTrack first if needed.
# "--background" (used at login and by the installer) starts it without a window.
#
# Two separate things:
#  - the tracker (run.sh) is started by macOS's launchd, NOT by this app. Otherwise macOS counts the
#    app as still open while the tracker runs, and clicking the app again does nothing.
#  - the window runs as this app's own process (exec): the app is open exactly while the window is.
APP_DIR="$HOME/Library/Application Support/LazyTrack"
PLIST="$HOME/Library/LaunchAgents/com.lazytrack.tracker.plist"
mkdir -p "$HOME/.lazytrack" "$HOME/Library/LaunchAgents"
if ! curl -s -m 2 -o /dev/null http://127.0.0.1:8042/api/auth/state; then
  # Settings > App > "Start with your computer" off: the tracker doesn't start by itself at the next login
  AT_LOGIN="true"; [ -f "$HOME/.lazytrack/no-autostart" ] && AT_LOGIN="false"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.lazytrack.tracker</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$APP_DIR/mac/run.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>LAZYTRACK_RESTARTED</key><string>1</string></dict>
  <key>RunAtLoad</key><$AT_LOGIN/>
  <key>KeepAlive</key><false/>
</dict></plist>
PL
  U="gui/$(id -u)"
  launchctl bootstrap "$U" "$PLIST" 2>/dev/null
  launchctl kickstart "$U/com.lazytrack.tracker" 2>/dev/null || {
    # launchd said no: start it the old way, so LazyTrack still runs
    LAZYTRACK_RESTARTED=1 nohup /bin/bash "$APP_DIR/mac/run.sh" >/dev/null 2>&1 & disown
  }
fi
[ "$1" = "--background" ] && exit 0
cd "$APP_DIR" && exec "$APP_DIR/.venv/bin/python" -m lazytrack window >>"$HOME/.lazytrack/window.log" 2>&1
