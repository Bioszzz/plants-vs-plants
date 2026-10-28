@echo off
setlocal
cd /d "%~dp0"
title 植物斗争 - 检查更新

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
echo 正在检查更新（对比 Gitee 上的公开发行版）...
echo.

"%PYEXE%" updater.py

echo.
echo 已退出。
pause
