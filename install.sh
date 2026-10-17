#!/bin/bash
# One-time setup. Run from this folder: ./install.sh
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -z "$PY" ] && { echo "No python3 found. Install it with: xcode-select --install"; exit 1; }
echo "Using $($PY --version)"

$PY -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# ultralytics drags in opencv-python-headless, which breaks cv2 when both are installed -> keep exactly one
.venv/bin/pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python opencv-contrib-python-headless
.venv/bin/pip install opencv-python
chmod +x start.command lt
./lt init

echo
echo "Done! Next:"
echo "  1. ./lt check      -> grants permissions + tests everything"
echo "  2. ./lt camtest    -> see what the camera detects (q to quit)"
echo "  3. double-click start.command (or ./lt run) and open http://localhost:8042"
