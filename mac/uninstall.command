#!/bin/bash
# Removes LazyTrack from this Mac. Your data (~/.lazytrack) stays unless you say so.
echo "This removes LazyTrack from your Mac."
read -r -p "Continue? (y/n) " a; [ "$a" = "y" ] || exit 0
pkill -f "Application Support/LazyTrack/.venv/bin/python" 2>/dev/null
launchctl unload "$HOME/Library/LaunchAgents/com.lazytrack.app.plist" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.lazytrack.app.plist"
rm -rf "$HOME/Applications/LazyTrack.app"
read -r -p "Also delete your data (logs, stats, account on this Mac)? (y/n) " d
[ "$d" = "y" ] && rm -rf "$HOME/.lazytrack"
rm -rf "$HOME/Library/Application Support/LazyTrack"
echo "LazyTrack is removed."
