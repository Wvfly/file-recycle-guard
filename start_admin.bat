@echo off
echo ========================================
echo   文件回收站守护程序 - 管理员模式启动
echo ========================================
echo.
echo 正在以管理员权限启动...

powershell -Command "Start-Process python -ArgumentList 'main.py start' -WorkingDirectory '%~dp0' -Verb RunAs"

echo.
echo 请在弹出的 UAC 对话框中点击"是"确认管理员权限。
echo 启动后会自动打开一个管理员命令行窗口。
pause
