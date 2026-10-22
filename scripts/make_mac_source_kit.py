"""Pack a "build kit" that produces EnzyDock.app on a Mac.

PyInstaller cannot cross-compile: a macOS ``.app`` only exists once it has
been built *on* a Mac, with a same-architecture Python.  So handing somebody
a macOS client means handing them the source plus one command - and that
command has to be double-clickable, because the person receiving it is a
scientist, not a build engineer.

What lands in the archive::

    EnzyDock-mac-build-kit-<version>/
    |-- 先看我-READ-ME-FIRST.txt       what this is, what to double-click
    |-- 双击我构建 EnzyDock.command    runs scripts/build_mac.sh
    |-- BUILD-KIT-MANIFEST.txt         file count + SHA256 of the kit itself
    `-- EnzyDock/                      the source tree (venv/dist excluded)

Usage::

    python scripts/make_mac_source_kit.py
    python scripts/make_mac_source_kit.py --out dist/kit
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import __version__                                # noqa: E402

#: Directories that must never travel: a virtual environment is ~1 GB and is
#: rebuilt on the Mac anyway; ``dist``/``build`` hold *other* platforms'
#: binaries; ``.workbuddy`` is this checkout's private notes.
EXCLUDED_DIRS = frozenset({
    ".venv", "venv", "dist", "build", "workspace", "__pycache__",
    ".git", ".pytest_cache", ".mypy_cache", ".workbuddy", ".idea",
    ".vscode", "node_modules",
})

#: Single files that are either platform-specific junk or private.
EXCLUDED_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

SOURCE_DIR = "EnzyDock"
KIT_NAME = f"EnzyDock-mac-build-kit-{__version__}"

READ_ME = """\
EnzyDock macOS 版 —— 一键构建包（{version}）
================================================================

这不是安装程序，Mac 版的 EnzyDock 必须在 Mac 上现场生成。

PyInstaller 不能跨平台编译，所以 Windows 电脑没法提前做好 .app 发给你。
这个包里是**完整的源代码 + 一条命令**，在你的 Mac 上跑一次就能得到
可以双击使用的 EnzyDock.app。


你需要什么
----------------------------------------------------------------
  1. 一台 Mac（Apple Silicon M1/M2/M3/M4 或 Intel 都可以）
  2. macOS 12 以上
  3. 能联网（第一次构建要下载 Python 依赖，约 300~600 MB）
  4. 大约 15~25 分钟（第一次；之后再构建约 3 分钟）


怎么做（一共两步）
----------------------------------------------------------------
  第 1 步：把整个文件夹从 zip 里解压出来，放到一个**路径不含中文**的地方
           （推荐直接放在「下载」或桌面上，不要放在微信的接收目录里，
           那里随时会被清理）。

  第 2 步：双击  "双击我构建 EnzyDock.command"

           ★ 第一次双击时 macOS 会拦截它（和 EnzyDock.app 一样的原因）。
             这时右键点它 →「打开」→ 再点一次「打开」即可。

           ★ 如果双击后它是被「文本编辑」打开的，而不是弹出终端窗口，
             说明解压工具没保留可执行属性。在「终端」里执行：
                 cd 解压出来的目录
                 chmod +x 双击我构建*.command
             然后再双击一次。

           然后会弹出一个「终端」窗口，自动完成：
             建虚拟环境 → 装依赖 → 生成图标 → 跑测试 → 构建 .app → 自检
           中途会问你一次开机密码（装 Xcode 命令行工具），输完回车。
           看到  Done.  就算成功。


成功后你会得到
----------------------------------------------------------------
    解压目录/EnzyDock/dist/EnzyDock.app        ← 拖进「应用程序」就能用
    解压目录/EnzyDock/dist/EnzyDock-<ver>-macos-arm64.zip   ← 发给别人的那个

  双击 EnzyDock.app 第一次仍然要右键 → 打开（同上，是 macOS 的规则）。


如果失败了
----------------------------------------------------------------
  终端窗口里会直接写出原因。最常见三种：

  · "xcode-select" / 编译相关
        → 终端里执行   xcode-select --install   按提示装完，再重新双击。

  · 下载很慢或超时
        → 是网络问题。重新双击一次即可（已经下好的不会重复下载）。
          如果在国内，可以把 pip 换成阿里云镜像：
          pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/

  · 提示 Python 版本不对
        → 需要 Python 3.10 以上。执行 python3 --version 看一眼；
          没有的话去 python.org 装一个 3.12。


包里有什么
----------------------------------------------------------------
    EnzyDock/app/            程序本体（GUI、化学、对接、3D 视图）
    EnzyDock/scripts/        构建与打包脚本（build_mac.sh 在这里）
    EnzyDock/packaging/      给最终用户的说明文件
    EnzyDock/app/resources/bin/vina_mac_aarch64
                             内置的 AutoDock Vina 1.2.7（Apple Silicon 版，
                             Intel 版也在同一个目录）—— 不需要另外装对接引擎
    EnzyDock/examples/       示例蛋白，自检时会用到
    EnzyDock/tests/          测试套件，构建前会自动跑一遍

  有问题把终端窗口里的最后 30 行发给打包给你的人。
"""

COMMAND_SCRIPT = """\
#!/bin/sh
# ===========================================================================
#  双击这个文件，在 Mac 上构建 EnzyDock。
#
#  它会检查环境、装好依赖，然后执行 scripts/build_mac.sh。
#  全程在「终端」窗口里显示进度，看到 "Done." 就是成功了。
# ===========================================================================

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/EnzyDock"

say() { printf '\\n=== %s\\n' "$1"; }

if [ ! -d "$SRC" ]; then
    say "找不到 $SRC"
    echo "请把整个文件夹完整解压出来，不要只拿出这一个文件。"
    echo "按回车键关闭…"
    read -r _
    exit 1
fi

cd "$SRC" || exit 1

say "检查运行环境"
echo "  目录   : $SRC"
echo "  系统   : $(sw_vers -productVersion 2>/dev/null || echo '?')"
echo "  芯片   : $(uname -m)"

if ! command -v python3 >/dev/null 2>&1; then
    say "没有找到 python3"
    echo "请先安装 Python 3.12：https://www.python.org/downloads/"
    echo "（安装程序里记得勾选 'Install for all users'，或装完重启终端。）"
    echo "按回车键关闭…"
    read -r _
    exit 1
fi

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "  Python : $PYVER"
MAJOR="$(echo "$PYVER" | cut -d. -f1)"
MINOR="$(echo "$PYVER" | cut -d. -f2)"
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
    say "Python 版本太低：$PYVER"
    echo "EnzyDock 需要 Python 3.10 以上（推荐 3.12）。"
    echo "按回车键关闭…"
    read -r _
    exit 1
fi

# PyInstaller 的引导程序需要编译，Xcode 命令行工具提供编译器。
if ! xcode-select -p >/dev/null 2>&1; then
    say "首次构建需要 Xcode 命令行工具"
    echo "接下来会弹出一个安装窗口，点「安装」并同意许可协议。"
    echo "装完以后**重新双击本文件**。"
    echo "按回车键开始安装…"
    read -r _
    xcode-select --install || true
    echo "按回车键关闭…"
    read -r _
    exit 1
fi

say "开始构建（第一次约 15~25 分钟，请保持联网）"
sh scripts/build_mac.sh

say "构建完成"
echo
echo "  应用程序 ：$SRC/dist/EnzyDock.app"
echo "  发给别人 ：$SRC/dist/ 目录下的 .zip"
echo
echo "按回车键打开 dist 文件夹…"
read -r _
open "$SRC/dist" 2>/dev/null || true
"""


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def iter_source_files(root: Path):
    """Every file worth shipping, newest-independent of host OS."""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.name in EXCLUDED_FILES:
            continue
        yield path, relative


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def write_kit(destination: Path, compresslevel: int = 6) -> tuple[Path, int, int]:
    """Build ``destination/<KIT_NAME>.zip`` and return (path, files, bytes)."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{KIT_NAME}.zip"
    if archive.is_file():
        archive.unlink()

    sources = list(iter_source_files(PROJECT_ROOT))
    written = 0
    total = 0

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=compresslevel) as zf:
        # The two entry points first, so an unzip-and-look shows them.
        note = zipfile.ZipInfo(f"{KIT_NAME}/先看我-READ-ME-FIRST.txt",
                               date_time=datetime.now().timetuple()[:6])
        note.external_attr = (0o100644 << 16)
        note.create_system = 3                     # 3 = Unix
        zf.writestr(note, READ_ME.format(version=__version__))

        command = f"{KIT_NAME}/双击我构建 EnzyDock.command"
        info = zipfile.ZipInfo(command, date_time=datetime.now().timetuple()[:6])
        # 0755 on the entry: without it Finder opens the file in TextEdit
        # instead of running it.
        info.external_attr = (0o100755 << 16)
        info.create_system = 3
        zf.writestr(info, COMMAND_SCRIPT)

        for index, (path, relative) in enumerate(sources, start=1):
            payload = path.read_bytes()
            name = f"{KIT_NAME}/{SOURCE_DIR}/{relative.as_posix()}"
            if relative.suffix in (".sh", ".command"):
                # A shell script without the executable bit cannot be run
                # directly on macOS ("Permission denied").  ``writestr``
                # stores 0600, so set the mode on the entry itself.
                entry = zipfile.ZipInfo(name,
                                        date_time=datetime.now().timetuple()[:6])
                entry.external_attr = (0o100755 << 16)
                entry.create_system = 3
                zf.writestr(entry, payload)
            else:
                zf.writestr(name, payload)
            written += 1
            total += len(payload)
            if index % 100 == 0 or index == len(sources):
                print(f"  packed {index}/{len(sources)} files")

    # Re-open before verifying: testzip() on a writer reads buffered bytes.
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        members = len(zf.namelist())
    if bad is not None:                                   # pragma: no cover
        raise RuntimeError(f"the kit is corrupt at {bad}")
    print(f"  verified: {members} members, CRCs all match")
    return archive, written, total


def write_manifest(destination: Path, archive: Path, files: int,
                   total: int) -> Path:
    lines = [
        f"EnzyDock {__version__} - macOS build kit",
        "=" * 58,
        "",
        f"Packed       : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Source files : {files} ({human(total)})",
        f"Archive      : {archive.name}",
        f"Size         : {archive.stat().st_size} bytes "
        f"({human(archive.stat().st_size)})",
        f"SHA256       : {sha256(archive)}",
        "",
        "How to check a copy on the receiving Mac",
        "----------------------------------------",
        "    shasum -a 256 " + archive.name,
        "",
        "What the receiver does",
        "----------------------",
        "  1. unzip the whole folder (not just one file out of it);",
        "  2. double-click  '双击我构建 EnzyDock.command'",
        "     (right-click -> Open the first time);",
        "  3. wait for 'Done.', then drag dist/EnzyDock.app to /Applications.",
        "",
        "Why this is a build kit and not an app",
        "--------------------------------------",
        "  PyInstaller cannot cross-compile. A macOS .app can only be built",
        "  on a Mac, so the source plus one command is the smallest thing",
        "  that reliably becomes a working client there.",
        "",
    ]
    manifest = destination / f"{KIT_NAME}-MANIFEST.txt"
    # LF, always: ``Path.write_text`` would use the host's line ending, and
    # a CRLF here breaks ``sha256sum -c`` on the file name.
    manifest.write_bytes("\n".join(lines).encode("utf-8"))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pack the source as a one-click macOS build kit")
    parser.add_argument("--out", type=Path,
                        default=PROJECT_ROOT / "dist" / "kit",
                        help="where the archive and manifest are written")
    parser.add_argument("--compresslevel", type=int, default=6,
                        help="zip deflate level 0-9 (default 6)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print(f"Building {KIT_NAME} from {PROJECT_ROOT}")
    archive, files, total = write_kit(args.out, args.compresslevel)
    manifest = write_manifest(args.out, archive, files, total)

    print()
    print(f"  source files : {files} ({human(total)})")
    print(f"  archive      : {archive} ({human(archive.stat().st_size)})")
    print(f"  manifest     : {manifest}")
    print()
    print("Send the archive.  The receiver unzips it and double-clicks")
    print("'双击我构建 EnzyDock.command'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
