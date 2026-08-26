@echo off
rem StockManager 工作台 一键启动(Windows 双击入口)
rem 放到桌面双击即可。若项目路径不同,修改下面 cd 那一行。
chcp 65001 >nul
cd /d "D:\path\to\StockManager\stock_analysis_based_on_baostock_2025_09_19"

python scripts\launcher.py start

echo.
echo ------------------------------------------------------------
echo 启动器已结束。按任意键关闭本窗口。
pause >nul
