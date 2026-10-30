#!/bin/sh
# ======================================================================
#  植物斗争 —— 检查更新（Linux / Android Termux 版）
#
#  等价于 Windows 版 `检查更新.bat`：真正干活的只有一条
#      python updater.py
#
#  用法：
#      sh 检查更新.sh                  # 查 → 问一句 → 下载校验备份覆盖
#      sh 检查更新.sh --dry-run        # 只查、只校验，一个文件都不覆盖
#      sh 检查更新.sh --yes            # 不提问，直接装
#
#  ★ 与 .bat 唯一的区别（故意的）：当前目录是 git 源码仓库（有 .git）时，
#    本脚本不会直接跑 updater.py —— 那段输出里写了原因和替代命令。
# ======================================================================
set -u

# 切到脚本自己所在的目录：从任何地方调用都行，用 `sh 文件名` 不需要可执行位
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$here" || exit 1

# ---------- 找 Python ----------
PY=""
for c in "${PVP_PYTHON:-}" python3 python; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
    echo "[错误] 没找到 Python 3。装一个再来："
    echo "    Termux ：pkg install python"
    echo "    Debian ：sudo apt install python3"
    exit 1
fi

# ---------- 摘出 --updater 开关，其余参数原样传给 updater.py ----------
# POSIX sh 没有数组，所以"从队头 shift、往队尾 set -- 追加"重排一遍。
USE_UPDATER=0
_n=$#
_i=0
while [ "$_i" -lt "$_n" ]; do
    _a=$1
    shift
    _i=$((_i + 1))
    case "$_a" in
        --updater) USE_UPDATER=1 ;;
        *) set -- "$@" "$_a" ;;
    esac
done

# ---------- git 源码仓库：先拦一下 ----------
if [ -d .git ] && [ "$USE_UPDATER" -eq 0 ]; then
    echo "检测到 .git —— 这个目录是【源码仓库】，不是解压出来的发行包。"
    echo
    echo "  updater.py 会把发行 zip 里的文件直接覆盖到当前目录（旧文件另存到"
    echo "  _backup/）。在源码仓库里这么干，你的改动会和发行版混在一起，"
    echo "  git status 一片红，下次 pull 还可能冲突。所以这里先停下。"
    echo
    echo "  源码更新：      git pull --ff-only"
    echo "  仍要用更新器：  sh 检查更新.sh --updater --dry-run    # 先只看会覆盖什么"
    exit 0
fi

exec "$PY" updater.py "$@"
