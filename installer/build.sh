#!/bin/bash
# Builds the installers into dist/:
#   LazyTrack-Setup.exe          Windows (64-bit)
#   Install-LazyTrack-Mac.zip    Mac (Apple Silicon + Intel): unzip, open "Install LazyTrack"
# The app files come from this git checkout (what's committed + new files that aren't ignored);
# VERSION is set to the current commit so the updater knows what's installed.
set -e
cd "$(dirname "$0")/.."
ROOT="$PWD"
rm -rf dist && mkdir -p dist

python3 - <<'EOF'
import subprocess, zipfile, os
files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], capture_output=True, text=True, check=True).stdout.split()
skip = ("installer/", "ios/", "dist/", ".git")
sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
with zipfile.ZipFile("installer/payload.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(set(files)):
        if f.startswith(skip) or not os.path.isfile(f) or "__pycache__" in f:
            continue
        info = zipfile.ZipInfo.from_file(f, f)
        info.compress_type = zipfile.ZIP_DEFLATED
        with open(f, "rb") as fh:
            z.writestr(info, fh.read())
    z.writestr("VERSION", sha + "\n")
print("payload:", len(z.namelist()) if False else "ok", os.path.getsize("installer/payload.zip") // 1024, "KB, version", sha[:7])
EOF

cd installer
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -H windowsgui" -o "$ROOT/dist/LazyTrack-Setup.exe" .
GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$ROOT/dist/installer-arm64" .
GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$ROOT/dist/installer-x86_64" .
cd "$ROOT"

# the Mac installer is a small app: "Install LazyTrack.app" picks the right program for the Mac's chip
python3 - <<'EOF'
import zipfile, os, stat
app = "Install LazyTrack.app/Contents"
plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Install LazyTrack</string>
  <key>CFBundleIdentifier</key><string>com.lazytrack.installer</string>
  <key>CFBundleExecutable</key><string>install</string>
  <key>CFBundleIconFile</key><string>lazytrack</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
"""
launcher = """#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
if [ "$(uname -m)" = "arm64" ]; then exec "$DIR/installer-arm64" "$@"; else exec "$DIR/installer-x86_64" "$@"; fi
"""
def add(z, name, data, mode):
    zi = zipfile.ZipInfo(name); zi.external_attr = (stat.S_IFREG | mode) << 16; zi.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(zi, data)
with zipfile.ZipFile("dist/Install-LazyTrack-Mac.zip", "w") as z:
    add(z, f"{app}/Info.plist", plist, 0o644)
    add(z, f"{app}/MacOS/install", launcher, 0o755)
    add(z, f"{app}/Resources/lazytrack.icns", open("assets/lazytrack.icns", "rb").read(), 0o644)
    for a in ("arm64", "x86_64"):
        add(z, f"{app}/Resources/installer-{a}", open(f"dist/installer-{a}", "rb").read(), 0o755)
for a in ("arm64", "x86_64"):
    os.remove(f"dist/installer-{a}")
EOF
ls -la dist
