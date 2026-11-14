#!/bin/sh
# ======================================================================
#  植物斗争 —— 植物图鉴（Linux / Android Termux 版）
#
#  它就是 Windows 版 `图鉴.bat` 的等价物：真正干活的只有一条
#      python codex_server.py
#  .bat 里那二十行"找 python.exe"是给 Windows 写的，这里换成 POSIX 的
#  `command -v`；另外补两件只有手机上才需要的事（保持唤醒、不硬开浏览器）。
#
#  用法：
#      sh 图鉴.sh                       # 默认 8400
#      sh 图鉴.sh --port 8500           # 指定端口（占用会自动往后找）
#      PVP_PYTHON=/path/to/python sh 图鉴.sh   # 强制指定解释器
#
#  ★ 图鉴是【独立服务】，不是对局的一部分：
#      - 不用先开 webui.py，也不用连 netserver；没开局照样能翻
#      - 端口 8400，跟对局的 8000 / 试玩台的 8300 都不撞
#      - 关掉这个脚本图鉴就没了，不影响任何正在进行的对局
#
#  ★ 为什么不能直接跑 .bat：.bat 是 cmd.exe 的脚本语言，Termux 里没有 cmd.exe，
#    连第一行 `@echo off` 都不认识 —— 一个 bat 都执行不了。
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
# 静默返回 False），所以 Termux 下默认加 --no-browser，省掉那次注定失败的尝试，
# 直接把地址打印出来让人手点。
if [ "$#" -eq 0 ] && [ -n "${TERMUX_VERSION:-}" ]; then
    set -- --no-browser
fi

echo "使用 Python：$PY"
echo
echo "正在启动【植物图鉴】—— 独立页面，跟对局无关"
echo "  · 只监听 127.0.0.1 —— 只有本机能看，别的设备连不上"
echo "  · 默认端口 8400，被占用会自动往后找（不会抢对局的 8000）"
echo "  · 启动后按提示打开地址；手机上请长按/复制那个链接到浏览器"
echo
echo "  （Ctrl+C 退出；关掉它不影响任何正在进行的对局）"
echo

exec "$PY" codex_server.py "$@"
