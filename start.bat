@echo off
python _fix_crlf.py "%~f0" 2>nul
title 文件回收站守护程序

echo.
echo ============================================
echo   文件回收站守护程序 v1.0
echo   File Recycle Guard
echo ============================================
echo.

:: 切换到脚本所在目录
cd /d "%~dp0"

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请确保 Python 已安装并添加到 PATH
    pause
    exit /b 1
)

:: 安装依赖
echo [1/3] 检查依赖...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo       依赖检查完成

:: 创建必要的目录
echo [2/3] 创建目录...
if not exist "logs" mkdir logs
echo       目录检查完成

:: 启动服务
echo [3/3] 启动守护程序...
echo       监控路径: 请在 config.yaml 中配置 watch_paths
echo       备份目录: 请在 config.yaml 中配置 backup_dir
echo       回收站目录: 请在 config.yaml 中配置 recycle_dir
echo       Web管理界面: 请在 config.yaml 中配置 web.port
echo.
echo       按 Ctrl+C 停止服务
echo ============================================
echo.

python main.py start

pause
