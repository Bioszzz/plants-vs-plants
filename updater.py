# -*- coding: utf-8 -*-
"""updater.py —— 检查更新 / 自动安装（Gitee 公开发行版）

用法：
    python updater.py             检查 →（有新版就问一句）→ 下载 → 安装
    python updater.py --check     只检查：有新版的退出码 1，已最新 0
    python updater.py --yes       不询问，直接装（给脚本 / bat 用）
    python updater.py --dry-run   只下载 + 校验，绝不覆盖

它做什么：
    ① 问 Gitee 要公开发行版的最新 tag 和 zip 地址；
    ② 下载到内存 / 临时目录（270 KB 左右，几秒）；
    ③ **校验**：zip 里不许有绝对路径或 `..`；顶层只能有一个 `plant-vs-plant-vX.Y/`；
       必须带齐关键文件；解压到临时目录后还要真跑一次 `import engine`；
    ④ 备份即将被覆盖的旧文件到 `_backup/v旧_时间戳/`；
    ⑤ 逐个文件二进制覆盖（bat 是 GBK+CRLF，绝不能重新编码）；
    ⑥ 把所在文件夹名里的旧版本号换成新的（只改"本来就叫 ...-vX.Y"的，见五之二）；
    ⑦ 告诉你"重启才生效"。

★★ 三条铁律（照着写，别简化）：
  1. **只覆盖 .zip 里真实存在的文件**。绝不删任何东西 —— 你的 names.json、
     dist/、_backup/ 都不在包里，也就不会被碰到。
  2. **先校验再动手**。下载到的可能是个坏包 / 半截文件 / 别人的包；
     校验不过就退出，一个字节都不写。
  3. **先备份再覆盖**。覆盖失败的窗口只有几十毫秒，但真出事时
     `_backup/v旧_xxx/` 就是唯一能救回来的东西。

★ 为什么是"覆盖文件"而不是"换目录"：这个项目是解压即用的绿色包，
  用户可能把它放在任何地方、可能自己改过 names.json。原地覆盖是唯一
  不会把用户数据搞丢的方式。
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 与 webui.py 里保持一致（两个公开仓库之一；GitHub 那个是私有的，匿名查不到）
GITEE_REPO = "Bioszzz/plants-vs-plants"
API_LATEST = "https://gitee.com/api/v5/repos/%s/releases/latest" % GITEE_REPO
RELEASES_PAGE = "https://gitee.com/%s/releases" % GITEE_REPO

# 包里必须存在的文件 —— 少一个就说明这个 zip 不是本项目的可运行包。
# ★ updater.py 自己也在清单里：少了它，这个包一装上去，下次就没法自动更新了
#   （那种"更新一次就永远停在原地"的坑最难发现，所以在校验层就拦住）。
REQUIRED = ("engine.py", "plants_data.py", "main.py", "webui.py",
            "netclient.py", "netserver.py", "netproto.py", "bots.py",
            "console_ui.py", "playtest.py", "updater.py",
            "web/index.html", "web/app.js", "VERSION")

BACKUP_DIR = "_backup"
UA = "plant-vs-plant-updater"


# ======================================================================
# 一、版本号
# ======================================================================
def read_version(base=None):
    """读本地 VERSION。读不到返回空串（调用方自己决定怎么提示）。"""
    try:
        with io.open(os.path.join(base or BASE_DIR, "VERSION"),
                     encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ver_key(v):
    """版本号按【数字段】比较：v0.9 < v0.19 < v0.68 < v0.100。

    ★ 字符串比较会把 v0.9 判成大于 v0.19 —— 这个坑在 webui 里已经踩过一次。
    """
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums) if nums else (0,)


# ======================================================================
# 二、问 Gitee
# ======================================================================
def _pick_asset(assets, tag):
    """从 Gitee 的 assets 里挑出【我们上传的那个 zip】。挑不到返回 None。

    ★★★ 这里有个坑（实测踩到）：Gitee 会在 assets 里**额外塞两条平台自动生成的
      源码归档**，它们长得和真 asset 一模一样：
          {name: "plant-vs-plant-v0.68.zip", url: ".../releases/download/v0.68/..."}  ← 真的
          {name: "v0.68.zip",                url: ".../archive/refs/tags/v0.68.zip"}   ← 归档
          {name: "v0.68.tar.gz",             url: ".../archive/refs/tags/v0.68.tar.gz"} ← 归档
      归档的名字里【也含 tag】，所以按"名字里有 tag 就打高分"来挑，会挑中归档 ——
      第一版就是这么写的，结果下载下来的是源码归档（顶层目录名都不一样）。
      判据必须是**排除 `/archive/` 路径**，而不是看名字像不像。
    """
    best, best_score = None, -1
    for a in (assets or []):
        if not isinstance(a, dict):
            continue
        url = (a.get("browser_download_url") or "").strip()
        name = (a.get("name") or "").strip()
        if not url:
            continue
        if "/archive/" in url:              # ★ 平台自动附带的源码归档，不是发布物
            continue
        low = name.lower()
        if not (low.endswith(".zip") or url.lower().endswith(".zip")):
            continue
        score = 0
        if name.startswith("plant-vs-plant"):   # 我们打包时的命名规则
            score += 2
        if tag and tag in name:
            score += 1
        if score > best_score:
            best, best_score = a, score
    return best


def fetch_latest(timeout=15):
    """返回 dict(tag, notes, asset_url, asset_size, err)。

    ★ 失败一律变成 err 字段，绝不抛异常 —— 调用它的可能是网页请求线程。
    ★ Gitee 的坑一：查不到也常返回 200（contents 给 []、releases 给 null），
      所以判据必须是"解析出来有没有 tag_name"，不能只看状态码。
    ★ Gitee 的坑二：assets 里混着源码归档，见 _pick_asset 的注释。
    ★ Gitee 的坑三：asset 只给 name 和 url，**没有 size 字段** ——
      所以 asset_size 恒为 0，下载时的大小校验只能跳过；
      真正兜底的是"PK 头 + zip 结构 + import 试跑"这三道。
    """
    out = {"tag": "", "notes": "", "asset_url": "", "asset_size": 0, "err": ""}
    req = urllib.request.Request(API_LATEST, headers={
        "User-Agent": "%s/%s" % (UA, read_version() or "?")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        out["err"] = ("发布页上还没有发行版" if exc.code == 404
                      else "HTTP %s" % exc.code)
        return out
    except Exception as exc:
        out["err"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    try:
        data = json.loads(raw)
    except ValueError:
        out["err"] = "发行版信息不是合法 JSON"
        return out
    if not isinstance(data, dict) or not (data.get("tag_name") or "").strip():
        out["err"] = "发布页上还没有发行版"
        return out

    out["tag"] = data["tag_name"].strip()
    out["notes"] = (data.get("body") or "").strip()
    best = _pick_asset(data.get("assets"), out["tag"])
    if best is None:
        out["err"] = "这个发行版没有附带 zip（只有说明？）"
        return out
    out["asset_url"] = (best.get("browser_download_url") or "").strip()
    try:
        out["asset_size"] = int(best.get("size") or 0)
    except (TypeError, ValueError):
        out["asset_size"] = 0
    return out


# ======================================================================
# 三、下载
# ======================================================================
def download(url, timeout=60, progress=None, expect_size=0):
    """把 url 下到内存里返回 bytes。270 KB 的东西，不必落盘再读。

    ★ 大小对不上就当失败：Gitee 偶尔会返回一个 HTML 错误页（200 + 网页），
      不校验的话下一步解压会报"不是 zip"，看不出真正原因。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            if progress:
                progress(buf.tell(), total)
    data = buf.getvalue()
    if not data:
        raise ValueError("下载到的内容是空的")
    if expect_size and len(data) != expect_size:
        raise ValueError("大小不符：期望 %d 字节，实际 %d 字节"
                         % (expect_size, len(data)))
    if not data.lstrip()[:2] == b"PK":
        raise ValueError("下载到的不是 zip（可能是错误页）")
    return data


# ======================================================================
# 四、校验 + 解压
# ======================================================================
class BadPackage(Exception):
    """zip 不合法 —— 里面不是本项目的可运行包。"""


def inspect_zip(zip_path, expect_ver=""):
    """校验 zip 结构，返回 (顶层目录名, [(zip 内路径, 相对路径), ...])。

    拒绝：绝对路径、`..`、盘符、空 zip、多个顶层目录、缺关键文件。
    ★ 这一层就是防"zip slip"（`../../windows/system32/x`）和"下错包"。
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BadPackage("不是有效的 zip 文件")

    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise BadPackage("zip 里一个文件都没有")

        tops = set()
        for n in names:
            norm = n.replace("\\", "/")
            if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
                raise BadPackage("zip 里有绝对路径：%r" % n)
            parts = norm.split("/")
            if any(p == ".." for p in parts):
                raise BadPackage("zip 里有跳出目录的路径：%r" % n)
            if not parts[0]:
                raise BadPackage("zip 里有奇怪的路径：%r" % n)
            tops.add(parts[0])
        if len(tops) != 1:
            raise BadPackage("zip 顶层应该是唯一一个目录，实际有 %d 个：%s"
                             % (len(tops), "、".join(sorted(tops))))
        top = tops.pop()

        rels = []
        for n in names:
            norm = n.replace("\\", "/")
            rel = norm[len(top) + 1:] if norm.startswith(top + "/") else ""
            if rel:
                rels.append((n, rel))

        have = {r for _, r in rels}
        missing = [f for f in REQUIRED if f not in have]
        if missing:
            raise BadPackage("包里缺这些关键文件：%s" % "、".join(missing))

        if expect_ver:
            try:
                with zf.open([n for n, r in rels if r == "VERSION"][0]) as f:
                    got = f.read().decode("utf-8", "replace").strip()
            except (KeyError, IndexError, OSError):
                got = ""
            if got and ver_key(got) != ver_key(expect_ver):
                raise BadPackage("包里的 VERSION 是 %r，和发行版 %r 对不上"
                                 % (got, expect_ver))
    return top, rels


def extract_to(zip_path, dest_dir):
    """解压到指定目录（结构原样保留）。返回顶层目录名。"""
    top, _rels = inspect_zip(zip_path)
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.filename.endswith("/"):
                continue
            norm = info.filename.replace("\\", "/")
            target = os.path.join(dest_dir, *norm.split("/"))
            # 再挡一次：拼出来的绝对路径必须落在 dest_dir 里面
            if not os.path.abspath(target).startswith(os.path.abspath(dest_dir) + os.sep):
                raise BadPackage("解压路径越界：%r" % info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return top


def smoke_import(dir_path, timeout=90):
    """在解压出来的目录里真跑一次 import —— 把"包坏了"挡在覆盖之前。

    ★ 只 import 不启动：`import engine` 只做模块级定义，不会开服务、不读网络。
    ★ 用 sys.executable（跑 updater 的那个 Python），和用户实际运行的环境一致。
    """
    code = ("import engine, plants_data, netproto, bots, console_ui, "
            "netclient, netserver, webui, playtest, main, updater\n"
            "print('IMPORT_OK')\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=dir_path,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or "IMPORT_OK" not in (r.stdout or ""):
        raise BadPackage("解压出来的代码 import 不起来：\n%s"
                         % ((r.stderr or r.stdout or "").strip()[:800]))


# ======================================================================
# 五、安装（备份 + 覆盖）
# ======================================================================
def backup_files(base, rels, old_ver):
    """把即将被覆盖的旧文件复制到 _backup/v旧_时间戳/ 下，返回备份目录。

    ★ 只备份**存在且会被覆盖**的文件 —— 备份目录的大小≈变化量，
      不会每升一版就多 270 KB 的死重量。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "%s_%s" % (old_ver or "unknown", stamp)
    dest = os.path.join(base, BACKUP_DIR, name)
    n = 0
    for rel in rels:
        src = os.path.join(base, *rel.split("/"))
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
    return dest, n


def install(binary, base=None, expect_ver="", old_ver=""):
    """把 zip 的**字节**装到 base 目录。返回 dict(backup, files, top)。

    顺序（每一步都可以失败退出，且退出时磁盘仍是"全部旧"或"全部新"）：
        ① inspect_zip 校验结构 → ② 解压到临时目录 + import 试跑
        → ③ 备份将被覆盖的旧文件 → ④ 逐个二进制复制覆盖
    ★ 第 ④ 步才是唯一会改动正式目录的地方，前面三步都在别处做。
    """
    base = base or BASE_DIR
    tmp_root = os.path.join(base, "_update_tmp")
    if os.path.isdir(tmp_root):
        shutil.rmtree(tmp_root, ignore_errors=True)
    os.makedirs(tmp_root, exist_ok=True)

    zip_path = os.path.join(tmp_root, "download.zip")
    with open(zip_path, "wb") as f:
        f.write(binary)

    try:
        top, rels = inspect_zip(zip_path, expect_ver)
        stage = os.path.join(tmp_root, "stage")
        extract_to(zip_path, stage)
        smoke_import(os.path.join(stage, top))

        rel_list = [rel for _, rel in rels]
        backup, n_backup = backup_files(base, rel_list, old_ver)

        written = 0
        for _name, rel in rels:
            src = os.path.join(stage, top, *rel.split("/"))
            dst = os.path.join(base, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # ★ 二进制复制：bat 是 GBK+CRLF，任何"按文本读写"都会把它弄坏
            shutil.copyfile(src, dst)
            written += 1
        return {"backup": backup, "backup_files": n_backup,
                "files": written, "top": top}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


# ======================================================================
# 五之二、更新后把所在文件夹改名成新版本号
# ======================================================================
def rename_plan(base=None, new_ver=""):
    """算出"文件夹名里那个版本号该换成什么"。返回 (旧路径, 新路径) 或 None。

    只处理【名字里本来就带 vX.Y】的文件夹（如 `plant-vs-plant-v0.72`）——
    那是"解压出来的包"的命名，更新完让它跟上版本号，免得看着还以为没升。
    用户自己起的名字（"植物斗争""我的游戏"）**一律不动**，那是他的东西。
    """
    base = os.path.abspath(base or BASE_DIR)
    name = os.path.basename(base.rstrip("\\/"))
    m = re.search(r"v\d+(?:\.\d+)+", name)
    if not m or not new_ver:
        return None
    new_name = name[:m.start()] + new_ver + name[m.end():]
    if new_name == name:
        return None
    new_path = os.path.join(os.path.dirname(base), new_name)
    if os.path.exists(new_path):
        return None                      # 同名目录已存在，别去覆盖别人的
    return base, new_path


# 助手脚本：隔 0.5 秒试一次，直到那个窗口关掉（最多等 5 分钟）。
# 写成独立进程而不是线程 —— 要等的是"本进程退出"，同一进程里等不到。
_RENAME_HELPER = r'''
import os, sys, time
old, new = sys.argv[1], sys.argv[2]
deadline = time.time() + 300
while time.time() < deadline:
    if not os.path.exists(old):
        break
    try:
        os.rename(old, new)
        break
    except OSError:
        time.sleep(0.5)
'''


def schedule_rename(base=None, new_ver="", quiet=False):
    """把"改名"这件事交给一个脱离出去的助手进程去等。

    ★★ 为什么不能就地改：Windows 下，一个目录只要还是某个进程的**当前工作目录**
       就改不了名（cmd.exe 和 python 都算）。而更新完的那一刻，用户面前那个窗口
       的 CWD 正是它 —— 直接 os.rename 必然 PermissionError。
       所以：助手进程的 cwd 放在**父目录**，它不锁目标，等那个窗口关掉/程序重启
       之后（锁一松）立刻改掉。
    ★ 失败绝不影响任何东西：最坏结果只是名字没改上，游戏照常跑。
    """
    pair = rename_plan(base, new_ver)
    if not pair:
        return None
    old, new = pair
    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：不跟着本进程一起死，
        # 也不抢控制台（否则关窗口时会被一起带走）
        flags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(
            [sys.executable, "-c", _RENAME_HELPER, old, new],
            cwd=os.path.dirname(old) or None,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except Exception as exc:                       # noqa: BLE001
        if not quiet:
            print("  （文件夹改名没排上：%s —— 手动改成 %s 也行）"
                  % (exc, os.path.basename(new)))
        return None
    return old, new


# ======================================================================
# 六、命令行
# ======================================================================
def _progress(done, total):
    if total:
        sys.stdout.write("\r  下载中 %d%%（%d/%d 字节）"
                         % (done * 100 // total, done, total))
    else:
        sys.stdout.write("\r  下载中 %d 字节" % done)
    sys.stdout.flush()


def plan(base=None):
    """检查（不安装）。返回 (cur, info, has_update)。"""
    base = base or BASE_DIR
    cur = read_version(base)
    info = fetch_latest()
    has = bool(info["tag"]) and ver_key(info["tag"]) > ver_key(cur)
    return cur, info, has


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    only_check = "--check" in argv
    assume_yes = "--yes" in argv or "-y" in argv
    dry = "--dry-run" in argv

    print("植物斗争 —— 检查更新")
    print("-" * 46)
    cur, info, has = plan()
    print("  当前版本：%s" % (cur or "（读不到 VERSION）"))
    if info["err"]:
        print("  查不到更新信息：%s" % info["err"])
        print("  发布页：%s" % RELEASES_PAGE)
        return 2
    print("  最新版本：%s" % info["tag"])
    if not has:
        print("\n已是最新，不用更新。")
        return 0
    if only_check:
        print("\n有新版本 %s（当前 %s）。" % (info["tag"], cur))
        if info["notes"]:
            print("-" * 46)
            print(info["notes"][:1500])
        return 1

    if info["notes"]:
        print("-" * 46)
        print(info["notes"][:1500])
    print("-" * 46)

    if not assume_yes:
        try:
            ans = input("要更新到 %s 吗？[Y/n] " % info["tag"]).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 3
        if ans and ans not in ("y", "yes", "是", "1"):
            print("已取消。")
            return 3

    try:
        print("\n下载 %s …" % info["asset_url"])
        binary = download(info["asset_url"], progress=_progress,
                          expect_size=info["asset_size"])
        print("\n  下载完成：%d 字节" % len(binary))

        if dry:
            # 只校验，不覆盖
            tmp = os.path.join(BASE_DIR, "_update_tmp")
            os.makedirs(tmp, exist_ok=True)
            try:
                p = os.path.join(tmp, "dry.zip")
                with open(p, "wb") as f:
                    f.write(binary)
                top, rels = inspect_zip(p, info["tag"])
                stage = os.path.join(tmp, "stage")
                extract_to(p, stage)
                smoke_import(os.path.join(stage, top))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            print("校验通过（--dry-run：没有覆盖任何文件）。")
            return 0

        print("校验 + 备份 + 安装 …")
        r = install(binary, expect_ver=info["tag"], old_ver=cur)
        print("  覆盖 %d 个文件（备份 %d 个 → %s）"
              % (r["files"], r["backup_files"], r["backup"]))
        print("\n已更新到 %s。" % info["tag"])
        rp = schedule_rename(new_ver=info["tag"])
        if rp:
            print("  这个文件夹会在窗口关掉后自动改名：%s → %s"
                  % (os.path.basename(rp[0]), os.path.basename(rp[1])))
        print("★ 请【关掉程序重新打开】才生效 —— 现在跑的仍然是旧代码。")
        return 0
    except BadPackage as exc:
        print("\n[失败] 这个包不能用：%s" % exc)
        print("没有改动任何文件。")
        return 4
    except Exception as exc:
        print("\n[失败] %s: %s" % (type(exc).__name__, exc))
        print("如果已经覆盖了一部分，旧文件在 _backup/ 下。")
        return 5


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
