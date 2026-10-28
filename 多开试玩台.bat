@echo off
setlocal
cd /d "%~dp0"
title 植物斗争 - 多开试玩台

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
echo 正在启动【本地试玩台】，浏览器会自动打开配置页...
echo   （配置页默认 8300 端口，被占用会自动往后找）
echo.
echo 在页面上配好 人数 / 电脑数 / 开局种子 / 游戏端口 / 座位名 / 房间规则，
echo 再点「一键开局」：服务端和 N 个座位页一起就绪，并自动打开 N 个标签。
echo 每个标签就是一个真人（都是你自己），谁在等答案各看各的页面。
echo.

"%PYEXE%" playtest.py

echo.
echo 已退出。
pause
