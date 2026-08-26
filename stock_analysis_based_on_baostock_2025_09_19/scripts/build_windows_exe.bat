@echo off
rem ============================================================
rem 在 Windows 上把 StockManager 打包成单个 exe(需在 Windows 机器执行)
rem 用法:双击本文件(scripts\build_windows_exe.bat)
rem 产物: dist\StockManager.exe —— 复制到任意文件夹双击即可运行,
rem       自动启动服务并打开浏览器,首次会自动回补历史数据。
rem ============================================================
chcp 65001 >nul
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "VENVPY=%ROOT%\.venv\Scripts\python.exe"

if exist "%VENVPY%" (
  echo 使用解释器: %VENVPY%
  "%VENVPY%" -m pip install --upgrade pyinstaller || goto :fail
  echo 正在打包(onefile, 无控制台窗口)...
  "%VENVPY%" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name StockManager ^
    --add-data "config\rule_templates;config\rule_templates" ^
    --add-data "config\sync.json;config" ^
    --add-data "src\stock_manager\web\static;src\stock_manager\web\static" ^
    scripts\exe_entry.py
) else (
  echo 未找到 .venv,改用 py -3(先安装项目依赖)...
  py -3 -m pip install -e . || goto :fail
  py -3 -m pip install --upgrade pyinstaller || goto :fail
  echo 正在打包(onefile, 无控制台窗口)...
  py -3 -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name StockManager ^
    --add-data "config\rule_templates;config\rule_templates" ^
    --add-data "config\sync.json;config" ^
    --add-data "src\stock_manager\web\static;src\stock_manager\web\static" ^
    scripts\exe_entry.py
)
if errorlevel 1 goto :fail

echo.
echo 完成: %ROOT%\dist\StockManager.exe
echo 把它复制到任意文件夹,双击即可启动并自动打开浏览器。
pause
exit /b 0

:fail
echo 构建失败,请查看上方错误信息。
pause
exit /b 1
