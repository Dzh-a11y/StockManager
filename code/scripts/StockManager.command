#!/usr/bin/env bash
# StockManager 工作台 一键启动(macOS 双击入口)
# 保留在 scripts 目录中;可为此文件创建 Finder 替身放到桌面。
#
# 根据脚本所在目录定位代码目录,整个项目移动后无需修改路径。
set -e
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

/usr/bin/env python3 scripts/launcher.py start

echo
echo "————————————————————————————"
echo " 启动器已结束。查看上面的信息;按回车键关闭本窗口。"
read -r
