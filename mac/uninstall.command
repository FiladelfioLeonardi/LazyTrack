#!/bin/bash
# Removes LazyTrack from this Mac. Your data (~/.lazytrack) stays unless you say so.
echo "This removes LazyTrack from your Mac."
read -r -p "Continue? (y/n) " a; [ "$a" = "y" ] || exit 0
pkill -f "Application Support/LazyTrack/.venv/bin/python" 2>/dev/null
launchctl unload "$HOME/Library/LaunchAgents/com.lazytrack.app.plist" 2>/dev/null
launchctl bootout "gui/$(id -u)/com.lazytrack.tracker" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.lazytrack.app.plist" "$HOME/Library/LaunchAgents/com.lazytrack.tracker.plist"
rm -rf "$HOME/Applications/LazyTrack.app"
read -r -p "Also delete your data (logs, stats, account on this Mac)? (y/n) " d
# to the Trash, not gone for good: changed your mind? Put it back from the Trash before emptying it
[ "$d" = "y" ] && mv "$HOME/.lazytrack" "$HOME/.Trash/LazyTrack data $(date +%Y-%m-%d_%H-%M)"
rm -rf "$HOME/Library/Application Support/LazyTrack"
echo "LazyTrack is removed."
