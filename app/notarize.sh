#!/bin/bash
# DocLens 公证 + staple + DMG
# 前置(一次性, 见 README-PUBLISH.md):
#   1) App Store Connect 创建 App 专用密码
#   2) xcrun notarytool store-credentials DocLens-notary --apple-id xx --team-id xx --password xx
# 用法: ./notarize.sh [profile名, 默认 DocLens-notary]
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${1:-DocLens-notary}"
APP="../dist/DocLens.app"
ZIP="/tmp/DocLens-notary.zip"
DMG="../dist/DocLens.dmg"

[ -d "$APP" ] || { echo "缺 $APP, 先跑 ./build_dist.sh"; exit 1; }

echo "== 1) 检查签名是 Developer ID(非 ad-hoc) =="
SIG=$(codesign -dv "$APP" 2>&1 | grep "Authority=" | head -1 || true)
echo "   $SIG"
echo "$SIG" | grep -q "Developer ID" || { echo "错误: 当前是 ad-hoc 签名。先: DEV_ID='Developer ID Application: ...' ./build_dist.sh"; exit 1; }

echo "== 2) 压缩送审 =="
ditto -c -k --keepParent "$APP" "$ZIP"

echo "== 3) 提交公证 (数分钟到半小时) =="
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait

echo "== 4) Staple 公证票 =="
xcrun stapler staple "$APP"
spctl --assess --type execute "$APP" && echo "Gatekeeper: PASS ✓"

echo "== 5) 制作 DMG =="
rm -f "$DMG"
hdiutil create -volname "DocLens" -srcfolder "$APP" -ov -format UDZO "$DMG"
# DMG 本身也公证(可选但推荐, 用户挂载时即验)
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait || echo "warn: DMG 公证失败不影响使用"
xcrun stapler staple "$DMG" || true

echo "完成: $DMG"
echo "分发: 用户下载 DMG -> 拖入 Applications -> 双击无任何安全警告"
