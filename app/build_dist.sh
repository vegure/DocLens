#!/bin/bash
# DocLens 发布版组装 + 签名脚本
# 用法:
#   ./build_dist.sh            # ad-hoc 签名(本地测试)
#   DEV_ID="Developer ID Application: Your Name (TEAMID)" ./build_dist.sh
#                              # Developer ID 正式签名(公证前一步)
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="DocLens"
DIST_APP="../dist/${APP_NAME}.app"
DEV_ID="${DEV_ID:-}"   # 空 = ad-hoc

echo "== 0) 依赖检查 =="
[ -x dist/server-onedir/server ] || { echo "缺 dist/server-onedir/server, 先跑: python3.10 -m PyInstaller server-onedir.spec --noconfirm"; exit 1; }
[ -f ../work/index.html ] || { echo "缺 ../work/index.html"; exit 1; }
# 启动器二进制: 已有就用, 没有就现场编译(依赖只有 AppKit/WKWebView, swiftc 直出)
mkdir -p build
if [ ! -x build/DocLens ]; then
  echo "   编译 Launcher.swift -> build/DocLens"
  swiftc Launcher.swift -o build/DocLens
fi

echo "== 1) 组装 App 骨架 =="
rm -rf "$DIST_APP"
mkdir -p "$DIST_APP/Contents/MacOS" "$DIST_APP/Contents/Resources" "$DIST_APP/Contents/Frameworks"
cp build/DocLens "$DIST_APP/Contents/MacOS/$APP_NAME"
cp Info.plist "$DIST_APP/Contents/Info.plist"
cp AppIcon.icns "$DIST_APP/Contents/Resources/"
cp ../work/index.html "$DIST_APP/Contents/Resources/web/index.html" 2>/dev/null || {
  mkdir -p "$DIST_APP/Contents/Resources/web"; cp ../work/index.html "$DIST_APP/Contents/Resources/web/index.html"; }
mkdir -p "$DIST_APP/Contents/Resources/bin"
cp ../work/ocr_bin "$DIST_APP/Contents/Resources/bin/ocr_bin" 2>/dev/null || echo "warn: 无 ../work/ocr_bin, 沙盒外会退回 swiftc 编译(首次保存时慢)"

echo "== 2) 拷贝 onedir 后端 =="
mkdir -p "$DIST_APP/Contents/Resources/backend"
cp -R dist/server-onedir/. "$DIST_APP/Contents/Resources/backend/"

echo "== 3) 清理 macOS 元数据 + 可执行位规范 =="
find "$DIST_APP" -name "*.DS_Store" -delete
find "$DIST_APP" -name "._*" -delete
chmod +x "$DIST_APP/Contents/MacOS/$APP_NAME" "$DIST_APP/Contents/Resources/backend/server" "$DIST_APP/Contents/Resources/bin/ocr_bin" 2>/dev/null || true

echo "== 4) 签名 =="
SIGN_ARGS=(--force --deep --options runtime --entitlements entitlements.plist)
if [ -n "$DEV_ID" ]; then
  SIGN_ARGS+=(--sign "$DEV_ID")
  echo "   Developer ID 签名: $DEV_ID"
else
  SIGN_ARGS+=(--sign -)
  echo "   ad-hoc 签名(本地测试, 不可公证)"
fi
# --deep 从内到外签; Python .so 数量多, 耗时约 1-2 分钟
codesign "${SIGN_ARGS[@]}" "$DIST_APP"
echo "== 5) 验证 =="
codesign --verify --deep --strict --verbose=2 "$DIST_APP" 2>&1 | tail -3
spctl --assess --type execute "$DIST_APP" && echo "Gatekeeper: PASS" || echo "Gatekeeper: ad-hoc 版预期 FAIL(公证后才 PASS)"
du -sh "$DIST_APP"
echo "完成: $DIST_APP"
