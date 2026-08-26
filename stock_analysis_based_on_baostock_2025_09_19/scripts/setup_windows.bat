@echo off
rem ============================================================
rem StockManager 安装辅助(Windows 双击本文件)
rem 作用:在桌面生成带图标的 StockManager 快捷方式。
rem 其实第一次双击启动 StockManager.bat 也会自动生成,这里只是手动补救用。
rem ============================================================
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
echo.
echo 完成。现在可以双击桌面上的 StockManager 图标启动。
pause
