#!/usr/bin/env bash
# 构建 macOS 桌面应用 StockManager.app(双击图标即启动工作台,并自动打开浏览器)
# 用法: ./scripts/make_app.sh   → 在项目根生成 StockManager.app
#        ./scripts/make_app.sh /Applications   → 复制到指定目录
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

# 重建
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 可执行入口(把项目根路径写死进去)
exe="$APP/Contents/MacOS/$APP_NAME"
printf '#!/usr/bin/env bash\ncd "%s"\nexec /usr/bin/env python3 scripts/launcher.py start\n' "$ROOT" > "$exe"
chmod +x "$exe"

# 图标
cp "$ICON" "$APP/Contents/Resources/icon.icns"

# Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>StockManager</string>
  <key>CFBundleDisplayName</key><string>StockManager</string>
  <key>CFBundleIdentifier</key><string>local.stockmanager.workbench</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>StockManager</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
PLIST

echo "已生成: $APP"
echo "双击它即可启动工作台并自动打开浏览器(服务已在跑时只会重新打开浏览器)。"
