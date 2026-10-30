#!/bin/sh
# ======================================================================
#  植物斗争 —— 多开试玩台（Linux / Android Termux 版）
#
#  它就是 Windows 版 `多开试玩台.bat` 的等价物：真正干活的只有一条
#      python playtest.py
#  .bat 里那二十行"找 python.exe"是给 Windows 写的，这里换成 POSIX 的
#  `command -v`；另外补两件只有手机上才需要的事（保持唤醒、提示手点座位链接）。
#
#  用法：
#      sh 多开试玩台.sh                       # 默认
#      sh 多开试玩台.sh --port 8300           # 指定配置页端口
#      PVP_PYTHON=/path/to/python sh 多开试玩台.sh    # 强制指定解释器
#
#  ★ 为什么不能直接跑 .bat：.bat 是 cmd.exe 的脚本语言，Linux 里没有 cmd.exe，
#    连第一行 `@echo off` 都不认识 —— Termux 里一个 bat 都执行不了。
# ======================================================================
set -u

# 切到脚本自己所在的目录：这样从任何地方调用都行，用 `sh 文件名` 也不需要可执行位
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$here" || exit 1

# ---------- 找 Python ----------
# Termux（pkg install python）装完命令就叫 python；Debian / Ubuntu 上常只有 python3。
PY=""
for c in "${PVP_PYTHON:-}" python3 python; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
    echo "[错误] 没找到 Python 3。装一个再来："
    echo "    Termux ：pkg install python"
    echo "    Debian ：sudo apt install python3"
    echo "    Fedora ：sudo dnf install python3"
    exit 1
fi

# ---------- Termux：别让系统把进程杀掉 ----------
# 切到浏览器之后，Android 随时可能回收 Termux 的后台进程。termux-wake-lock
# 申请一个 wake lock 挡住这件事；没有这个命令（非 Termux）就跳过。
if command -v termux-wake-lock >/dev/null 2>&1; then
    if termux-wake-lock 2>/dev/null; then
        trap 'termux-wake-unlock 2>/dev/null' EXIT HUP INT TERM
        echo "（已申请 wake lock，退出本脚本时自动释放）"
    fi
fi

# ---------- 参数 ----------
# 手机上 webbrowser 打不开任何东西（Termux 里没有可用的浏览器命令，Python 会
# 静默返回 False），所以 Termux 下默认加 --no-browser，省掉那次注定失败的尝试。
if [ "$#" -eq 0 ] && [ -n "${TERMUX_VERSION:-}" ]; then
    set -- --no-browser
fi

echo "使用 Python：$PY"
echo
echo "正在启动【本地试玩台】…"
echo "  · 只监听 127.0.0.1 —— 只有本机能连，别的设备连不上"
echo "    （要跨设备联机请用 netserver.py / 房主控制台，那个监听 0.0.0.0）"
echo "  · 启动后在浏览器打开 http://127.0.0.1:8300/"
echo "    端口被占用会自动往后找，以 playtest 自己打印的那一行为准"
echo "  · 点「一键开局」；若一个标签都没自动打开，就在页面「座位」表里手点那几行链接，"
echo "    一个标签 = 一个真人"
echo
echo "  （Ctrl+C 退出）"
echo

exec "$PY" playtest.py "$@"
