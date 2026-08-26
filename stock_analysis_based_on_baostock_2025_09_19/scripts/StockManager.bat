@echo off
rem ============================================================
rem StockManager 工作台 一键启动(Windows 双击入口)
rem 放到桌面,双击即可:自动找 Python -> 自动建 .venv -> 自动装依赖
rem  -> 启动服务 -> 自动打开浏览器。
rem 假设本 .bat 位于项目的 scripts\ 目录下;若移动到别处,改下面 ROOT。
rem ============================================================
chcp 65001 >nul
setlocal

rem 项目根 = 本脚本所在目录的上一级(scripts\..)。改成固定路径亦可。
set "ROOT=%~dp0.."
cd /d "%ROOT%"

rem ---- 优先用项目自带 venv 的 python(与 mac 一致);否则找一个 Python 3.11+ ----
set "PYTHON="
set "VENVPY=%ROOT%\.venv\Scripts\python.exe"
if exist "%VENVPY%" (
  set "PYTHON=%VENVPY%"
) else (
  py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PYTHON=py -3"
  if not defined PYTHON python -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PYTHON=python"
  if not defined PYTHON python3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PYTHON=python3"
)
if not defined PYTHON (
  echo 未找到 Python 3.11 及以上版本。
  echo 请到 https://www.python.org/downloads/ 安装,并勾选 "Add Python to PATH"。
  pause
  exit /b 1
)

echo 使用解释器: %PYTHON%
echo 正在启动(首次会自动创建 .venv 并下载依赖,约 1-2 分钟)...
%PYTHON% scripts\launcher.py start

echo.
echo ------------------------------------------------------------
echo 启动器已结束。按任意键关闭本窗口。
pause >nul
