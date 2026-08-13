# 文件回收站守护程序 - 管理员启动脚本
# 自动检测是否需要提权，是则弹出 UAC 对话框

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# 检查是否已经是管理员
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")

if (-not $isAdmin) {
    Write-Host "[INFO] 需要管理员权限才能启用 USN Journal 监控，正在请求提权..."
    Write-Host "[INFO] 请在弹出的 UAC 对话框中点击 是/Yes"
    $args = "-ExecutionPolicy Bypass -NoProfile -File `"$PSCommandPath`""
    Start-Process PowerShell -ArgumentList $args -Verb RunAs -WorkingDirectory $scriptDir
    exit
}

# === 以下是管理员模式执行的代码 ===
Write-Host "========================================" -ForegroundColor Green
Write-Host "  文件回收站守护程序 v1.0 - 管理员模式" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

# 清理旧的 PID 文件
if (Test-Path "recycle_guard.pid") {
    Remove-Item "recycle_guard.pid" -Force
    Write-Host "[INFO] 已清理旧 PID 文件"
}

Write-Host "[INFO] 当前用户: $(whoami)"
Write-Host "[INFO] 工作目录: $(Get-Location)"

# 启动 Python
Write-Host "[INFO] 正在启动守护程序..." -ForegroundColor Yellow
python main.py start

Write-Host ""
Write-Host "守护程序已退出。" -ForegroundColor Red
Pause
