#!/usr/bin/env bash
# StockManager 工作台 一键启动(macOS 双击入口)
# 放到桌面 / 程序坞/Finder 中双击即可。
#
# 如果整个项目目录移动了,把下面这一行的路径改成新的项目路径即可。
set -e
cd "/Users/douzihao/StockManager/stock_analysis_based_on_baostock_2025_09_19"

/usr/bin/env python3 scripts/launcher.py start

echo
echo "————————————————————————————"
echo " 启动器已结束。查看上面的信息;按回车键关闭本窗口。"
read -r
