@echo off
chcp 65001 >nul
REM ============================================================
REM  Nuitka Onefile 打包脚本 - 文件回收站守护程序
REM  编译为单个 main.exe，config.yaml 放在 exe 旁边可修改
REM ============================================================

set PYTHON=C:\Users\wuweigang\.conda\envs\smb_recycle_py\python.exe

echo ========================================
echo  Nuitka Onefile 打包
echo ========================================
echo.

REM [1/4] 终止运行中的旧进程（避免 exe 被占用导致构建失败）
echo [1/4] 检查并终止旧进程...
for /f "tokens=2" %%a in ('tasklist /fi "IMAGENAME eq main.exe" /fo list 2^>nul ^| findstr /i "PID"') do (
    taskkill /f /pid %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul
echo       完成

REM [2/4] 清理 clcache 缓存（避免缓存损坏导致编译失败）
echo [2/4] 清理编译缓存...
if exist "%LOCALAPPDATA%\clcache" rmdir /s /q "%LOCALAPPDATA%\clcache" 2>nul
echo       完成

REM [3/4] 清理旧产物
echo [3/4] 清理旧构建产物...
if exist "dist" rmdir /s /q dist
if exist "main.build" rmdir /s /q main.build
if exist "main.dist" rmdir /s /q main.dist
if exist "output" rmdir /s /q output
if exist "nuitka-crash-report.xml" del /f "nuitka-crash-report.xml" 2>nul
echo       完成

REM [4/4] 开始编译（^ 续行不能在 if 括号块内使用，故放在外面）
echo [4/4] 开始编译（onefile 模式，耗时较长）...
echo.

%PYTHON% -m nuitka --onefile --output-dir=dist --include-package=core --include-package=web --include-data-dir=web/templates=web/templates --enable-plugin=anti-bloat --nofollow-import-to=service --windows-console-mode=attach --windows-company-name=FileRecycleGuard --windows-product-name=FileRecycleGuard --windows-file-version=1.0.0.0 --windows-product-version=1.0.0.0 --windows-file-description="File Recycle Guard Daemon" --assume-yes-for-downloads main.py

if %ERRORLEVEL% NEQ 0 goto :build_failed

REM 编译成功
if not exist "dist" mkdir dist
copy /Y config.yaml dist\config.yaml >nul

echo.
echo ========================================
echo  BUILD SUCCESS!
echo ========================================
echo.

REM 显示 exe 大小
for %%F in (dist\main.exe) do set SIZE_MB=%%~zF
set /a SIZE_MB=%SIZE_MB% / 1048576
echo  输出: dist\main.exe  (%SIZE_MB% MB^)
echo  配置: dist\config.yaml 已复制
echo.
echo  部署方式:
echo    将 dist\ 目录整体复制到目标服务器
echo    修改 config.yaml 中的路径等配置
echo    运行: main.exe start
echo.
echo  使用方式:
echo    main.exe start   - 启动守护程序
echo    main.exe stop    - 停止守护程序
echo    main.exe status  - 查看运行状态
echo    main.exe web     - 仅启动Web界面
echo.
goto :eof

:build_failed
echo.
echo ========================================
echo  BUILD FAILED!
echo ========================================
echo  请检查上方错误信息
echo.
exit /b 1
