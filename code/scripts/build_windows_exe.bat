@echo off
rem ============================================================
rem 在 Windows 上把 StockManager 打包成单个 exe(需在 Windows 机器执行)
rem 用法:双击本文件(scripts\build_windows_exe.bat)
rem 产物: dist\StockManager.exe —— 复制到任意文件夹双击即可运行,
rem       自动启动服务并打开浏览器,首次会自动回补历史数据。
rem 注意:本文件用 goto 流程,不用括号块,路径含空格/括号也安全。
rem ============================================================
chcp 65001 >nul
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "VENVPY=%ROOT%\.venv\Scripts\python.exe"
if exist "%VENVPY%" goto :with_venv
goto :without_venv

:with_venv
echo 使用解释器: %VENVPY%
rem 校验关键运行时依赖(含 backtrader 回测引擎)可导入;缺失则先装整套依赖。
"%VENVPY%" -c "import backtrader, baostock, tzdata" >nul 2>nul
if not errorlevel 1 goto :install_pyinstaller
echo 检测到依赖缺失(如 backtrader),正在安装项目依赖...
"%VENVPY%" -m pip install -e .
if errorlevel 1 goto :fail
:install_pyinstaller
"%VENVPY%" -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail
echo 正在打包(onefile, 无控制台窗口)...
rem --collect-submodules backtrader:强制收集 backtrader 全部子模块,
rem 避免其 from ... import * 通配导入在 PyInstaller 静态分析里被漏掉。
"%VENVPY%" -m PyInstaller --noconfirm --clean --onefile --noconsole --name StockManager --paths "src" --collect-submodules backtrader --add-data "config\rule_templates;config\rule_templates" --add-data "config\sync.json;config" --add-data "src\stock_manager\web\static;src\stock_manager\web\static" scripts\exe_entry.py
if errorlevel 1 goto :fail
goto :built

:without_venv
echo 未找到 .venv,改用 py -3(安装运行依赖)。
echo baostock 核心依赖 pandas 且只能在 Python 3.11 用轮子安装,
echo 这里用 --only-binary 强制轮子,绝不触发源码编译...
py -3 -m pip install --only-binary=:all: "pandas<3" baostock tzdata
if errorlevel 1 goto :fail
echo 安装回测引擎依赖 backtrader(纯 Python,不触发编译)...
py -3 -m pip install "backtrader>=1.9.78.123"
if errorlevel 1 goto :fail
py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail
echo 正在打包(onefile, 无控制台窗口)...
py -3 -m PyInstaller --noconfirm --clean --onefile --noconsole --name StockManager --paths "src" --collect-submodules backtrader --add-data "config\rule_templates;config\rule_templates" --add-data "config\sync.json;config" --add-data "src\stock_manager\web\static;src\stock_manager\web\static" scripts\exe_entry.py
if errorlevel 1 goto :fail
goto :built

:built
echo.
echo 完成: %ROOT%\dist\StockManager.exe
echo 把它复制到任意文件夹,双击即可启动并自动打开浏览器。
pause
exit /b 0

:fail
echo.
echo 构建失败,请查看上方错误信息。
pause
exit /b 1
