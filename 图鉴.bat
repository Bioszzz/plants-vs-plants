@echo off
setlocal
cd /d "%~dp0"
title 植物斗争 - 植物图鉴

rem ===== 找 Python（跟 多开试玩台.bat 同一套写法，两个文件不用各记一遍）=====
set "PYEXE="

rem 1) 安装版：%LOCALAPPDATA%\Programs\Python\PythonXX\python.exe
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%d\python.exe" set "PYEXE=%%d\python.exe"
)

rem 2) 装机盘根目录的绿色版
if not defined PYEXE (
    for /d %%d in ("%SystemDrive%\Python*") do (
        if exist "%%d\python.exe" set "PYEXE=%%d\python.exe"
    )
)

rem 3) 已经进了 PATH 的 py / python
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
    echo 先安装 Python 3，然后重新双击本文件。
    echo 下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo 使用 Python：%PYEXE%
echo.
echo 正在启动【植物图鉴】—— 独立页面，跟对局无关，没开局也能看...
echo   · 默认 8400 端口，被占用会自动往后找（不会抢对局的 8000）
echo   · 启动后自动打开浏览器；没打开就复制下面打印的地址
echo   · 只监听 127.0.0.1，别的设备连不上
echo.
echo 关掉这个窗口 = 关掉图鉴。
echo.

"%PYEXE%" codex_server.py

echo.
echo 图鉴已关闭。
pause
