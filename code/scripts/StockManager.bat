@echo off
rem ============================================================
rem StockManager 工作台 一键启动(Windows 双击入口)
rem 放到桌面,双击即可:自动找 Python -> 自动建 .venv -> 自动装依赖
rem  -> 启动服务 -> 自动打开浏览器。
rem 假设本 .bat 位于项目的 scripts\ 目录下;若移动到别处,改下面 ROOT。
rem 本文件用 goto 流程,不用括号块,路径含空格/括号也安全。
rem ============================================================
chcp 65001 >nul
setlocal

rem 项目根 = 本脚本所在目录的上一级(scripts\..)。改成固定路径亦可。
set "ROOT=%~dp0.."
cd /d "%ROOT%"

rem ---- 优先用项目自带 venv 的 python(与 mac 一致);否则找一个 Python 3.11+ ----
set "PYTHON="
set "VENVPY=%ROOT%\.venv\Scripts\python.exe"
if exist "%VENVPY%" goto :use_venv
goto :find_python

:use_venv
set "PYTHON=%VENVPY%"
echo 使用解释器: %PYTHON%
echo 正在启动(首次会自动创建 .venv 并下载依赖,约 1-2 分钟)...
"%PYTHON%" scripts\launcher.py start
goto :done

:find_python
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if defined PYTHON goto :run_cmd
python -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON=python"
if defined PYTHON goto :run_cmd
python3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON=python3"
if defined PYTHON goto :run_cmd
echo 未找到 Python 3.11 及以上版本。
echo 请到 https://www.python.org/downloads/ 安装,并勾选 "Add Python to PATH"。
pause
exit /b 1

:run_cmd
echo 使用解释器: %PYTHON%
echo 正在启动(首次会自动创建 .venv 并下载依赖,约 1-2 分钟)...
%PYTHON% scripts\launcher.py start

:done
echo.
echo ------------------------------------------------------------
echo 启动器已结束。按任意键关闭本窗口。
pause >nul
