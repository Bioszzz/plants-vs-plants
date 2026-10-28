@echo off
setlocal
cd /d "%~dp0"
title 植物斗争 - 房主控制台

rem ===== 找 Python：不依赖 PATH =====
set "PYEXE="

rem 1) 安装版：%LOCALAPPDATA%\Programs\Python\PythonXX\python.exe
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%d\python.exe" set "PYEXE=%%d\python.exe"
)

rem 2) 装在盘根目录的情况
if not defined PYEXE (
    for /d %%d in ("%SystemDrive%\Python*") do (
        if exist "%%d\python.exe" set "PYEXE=%%d\python.exe"
    )
)

rem 3) 最后才用 PATH 里的 py / python
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 set "PYEXE=py"
)
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    echo.
    echo [错误] 没找到 Python。
    echo 请先安装 Python 3，然后重新双击本文件。
    echo 下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo 使用 Python：%PYEXE%
echo.
echo 正在启动【房主控制台】，浏览器会自动打开控制台页面...
echo   （控制台默认 8090 端口，被占用会自动往后找）
echo.
echo 在控制台里配好 人数 / 电脑数 / 开局种子 / 游戏端口，再点「开局」。
echo 玩家用 网页版.bat 连到这台机器的 IP。
echo 房主自己【不下场】—— 只在控制台看战报和场面。
echo.

"%PYEXE%" netserver.py --open

echo.
echo 已退出。
pause
