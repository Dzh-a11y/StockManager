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

# 探测本机一个 Python 3.11+ 的 bin 目录,放入 LaunchServices 的 PATH。
# LaunchServices 启动的应用不继承终端 PATH,这里手动把新版 Python 放到最前,
# 这样 .app 里的 /usr/bin/env python3 会命中 3.11+ 而不是 macOS 自带的 3.9。
PY_BIN_DIR=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 \
     && "$c" -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" 2>/dev/null; then
    PY_BIN_DIR="$(dirname "$(command -v "$c")")"
    break
  fi
done
[ -n "$PY_BIN_DIR" ] || PY_BIN_DIR="/Library/Frameworks/Python.framework/Versions/Current/bin"

if [[ ! -f "$ICON" ]]; then
  echo "缺少图标,请先运行: python scripts/make_icon.py"
  exit 1
fi

# 重建
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 可执行入口(把项目根路径写死进去;日志写 data/launcher.log,失败时打开日志)
exe="$APP/Contents/MacOS/$APP_NAME"
cat > "$exe" <<EOF
#!/usr/bin/env bash
cd "${ROOT}"
mkdir -p data
LOG="data/launcher.log"
if /usr/bin/env python3 scripts/launcher.py start >> "\${LOG}" 2>&1; then
  exit 0
fi
echo "启动失败,日志: \${LOG}" >> "\${LOG}"
/usr/bin/env open "\${LOG}"
EOF
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
  <key>LSEnvironment</key>
  <dict>
    <key>PATH</key>
    <string>${PY_BIN_DIR}:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict></plist>
PLIST

echo "已生成: $APP"
echo "双击它即可启动工作台并自动打开浏览器(服务已在跑时只会重新打开浏览器)。"
