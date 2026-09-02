#!/usr/bin/env bash
# 构建 macOS 桌面应用 StockManager.app(双击图标即启动工作台,并自动打开浏览器)
# 用法: ./scripts/make_app.sh   → 在项目根生成 StockManager.app
#
# 用 osacompile 生成 AppleScript 应用:它的可执行文件是真正的 Mach-O,
# 能被 launchd/RBS 可靠启动(用 bash 脚本当 .app 主执行文件会 "launchd job spawn failed")。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="StockManager"
APP="$ROOT/$APP_NAME.app"
ICON="$ROOT/assets/icon/icon.icns"
VERSION="$("$ROOT/.venv/bin/python" -c 'import stock_manager; print(stock_manager.__version__)' 2>/dev/null || echo 0.0.0)"

if [[ ! -f "$ICON" ]]; then
  echo "缺少图标,请先运行: python scripts/make_icon.py"
  exit 1
fi

rm -rf "$APP"

# AppleScript 应用:运行启动器,输出写 data/launcher.log,失败时打开日志
PY="$ROOT/.venv/bin/python"
run_cmd="cd '$ROOT' && mkdir -p data && ('$PY' scripts/launcher.py start >> data/launcher.log 2>&1 || /usr/bin/open data/launcher.log)"
osacompile -o "$APP" -e "do shell script \"$run_cmd\""

# 图标:替换默认 applet.icns
rm -f "$APP/Contents/Resources/applet.icns"
cp "$ICON" "$APP/Contents/Resources/icon.icns"

# 元信息
PB="/usr/libexec/PlistBuddy"
PLIST="$APP/Contents/Info.plist"
add_or_set() {
  "$PB" -c "Add :$1 $2 $3" "$PLIST" 2>/dev/null || "$PB" -c "Set :$1 $3" "$PLIST"
}
add_or_set CFBundleIdentifier string local.stockmanager.workbench
add_or_set CFBundleIconFile string icon
add_or_set CFBundleVersion string "$VERSION"
add_or_set CFBundleShortVersionString string "$VERSION"
add_or_set CFBundleName string "$APP_NAME"
add_or_set CFBundleDisplayName string "$APP_NAME"

# 本地构建需 ad-hoc 签名;签名前清掉 Finder 扩展属性以免 codesign 报 detritus
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep --sign - "$APP"

echo "已生成: $APP"
echo "双击它即可启动工作台并自动打开浏览器(服务已在跑时只会重新打开浏览器)。"
echo "若首次双击被拦:右键 -> 打开;或终端执行: xattr -d com.apple.quarantine \"$APP\""
