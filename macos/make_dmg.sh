#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: DMG 只能在 macOS 上创建。"
  exit 1
fi

APP="dist/POFF.app"
OUT="dist/POFF-2.2-macOS.dmg"
STAGE="dist/dmg-root"

if [[ ! -d "$APP" ]]; then
  echo "ERROR: 找不到 $APP，请先运行 ./build_mac.sh"
  exit 1
fi

rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

hdiutil create \
  -volname "POFF" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  "$OUT"

rm -rf "$STAGE"
echo "DMG 已生成：$(pwd)/$OUT"
