#!/bin/bash
# Double-click to start LazyTrack. Add to System Settings > General > Login Items to autostart.
cd "$(dirname "$0")"
exec .venv/bin/python -m lazytrack run
