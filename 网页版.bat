@echo off
setlocal
cd /d "%~dp0"
title 植物斗争 - 网页版

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
echo 正在启动网页版客户端，浏览器会自动打开...
echo   （本地页面默认 8000 端口，被占用会自动往后找）
echo.
echo 打开后有两个选择：
echo   1) 填「服务器 IP」点连接  —— 加入一局别人的对局
echo      （房主先双击 房主控制台.bat，或运行 python netserver.py）
echo   2) 点「本机开一局」        —— 你和电脑打一局
echo      （人数 / 电脑 / 开局种子 都能在按钮上面填）
echo.

"%PYEXE%" webui.py

echo.
echo 已退出。
pause
