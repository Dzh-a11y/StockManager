# 在 Windows 桌面生成 StockManager 快捷方式(带图标,双击即启动)
# 用法(在项目根或 scripts\ 下打开 PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#
# 生成的快捷方式指向 scripts\StockManager.bat(控制台会显示安装/启动进度),
# 图标使用 assets\icon\icon.ico。项目位置移动后重新运行本脚本即可。
$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $PSScriptRoot                 # scripts\.. = 项目根
$BAT  = Join-Path $PSScriptRoot "StockManager.bat"
$ICO  = Join-Path $ROOT "assets\icon\icon.ico"
$DESKTOP = [Environment]::GetFolderPath("Desktop")
$LNK  = Join-Path $DESKTOP "StockManager.lnk"

if (-not (Test-Path $BAT)) {
  Write-Host "未找到 $BAT" -ForegroundColor Red
  exit 1
}

$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($LNK)
$s.TargetPath = $BAT
$s.WorkingDirectory = $ROOT
if (Test-Path $ICO) { $s.IconLocation = "$ICO,0" }
$s.Description = "StockManager 工作台一键启动"
$s.Save()

Write-Host "已生成桌面快捷方式: $LNK"
Write-Host "双击 StockManager 图标即可:自动装依赖 -> 启动服务 -> 打开浏览器。"
