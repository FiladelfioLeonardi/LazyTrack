#!/bin/bash
# Generates the Xcode project and opens it. Run from this folder: ./setup.sh
set -e
cd "$(dirname "$0")"

if ! command -v xcodegen >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Need Homebrew first: https://brew.sh (one command), then run ./setup.sh again."
    exit 1
  fi
  echo "Installing XcodeGen..."
  brew install xcodegen
fi

# unique bundle id (Apple needs one per developer)
USER_ID=$(whoami | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')
sed -i '' "s/com\.REPLACE_ME/com.${USER_ID}/g" project.yml

# try to find your Apple development team automatically
TEAM=$(security find-certificate -c "Apple Development" -p 2>/dev/null \
       | openssl x509 -noout -subject 2>/dev/null | sed -n 's/.*OU *= *\([A-Z0-9]\{10\}\).*/\1/p' | head -1)
if [ -n "$TEAM" ]; then
  sed -i '' "s/DEVELOPMENT_TEAM: \"[A-Z0-9]*\"/DEVELOPMENT_TEAM: \"${TEAM}\"/" project.yml
  echo "Using Apple team $TEAM"
fi

xcodegen generate
open LazyTrack.xcodeproj

echo
echo "In Xcode:"
echo "  1. Plug in your iPhone (and enable Developer Mode on it if asked)"
echo "  2. Click 'LazyTrack' in the left bar -> Signing & Capabilities -> pick your Team (your Apple ID)"
echo "  3. Select your iPhone at the top, press ▶"
