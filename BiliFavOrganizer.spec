# Build the self-contained Windows release package with:
#   pyinstaller --noconfirm --clean BiliFavOrganizer.spec
from pathlib import Path
import re
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(SPECPATH)
datas = [(str(ROOT / "static"), "static")]
binaries = []
hiddenimports = []

# Generated from the brand SVG masters by tools/brand/export_ico.py; commit the
# .ico so CI only needs requirements.txt + pyinstaller, not Pillow or Chromium.
APP_ICON = ROOT / "packaging" / "BiliFavOrganizer.ico"
if not APP_ICON.exists():
    raise FileNotFoundError(
        f"Missing application icon {APP_ICON}. Regenerate with: python tools/brand/export_ico.py"
    )

# 署名与版权写进 exe 属性（右键→属性→详细信息）。去掉它必须显式改这份 spec，
# 因此改名重打包的副本会留下痕迹，而正规产物始终能追到作者与仓库。
AUTHOR = "Martin-soaring-dev"
HOMEPAGE = "https://github.com/Martin-soaring-dev/bili-fav-organizer"
COPYRIGHT = f"© 2026 {AUTHOR} · PolyForm Noncommercial License 1.0.0 · {HOMEPAGE}"


def _release_metadata():
    """把 server.py 里的发布标记搬进 exe 版本资源；非 Windows 返回 None。"""
    if sys.platform != "win32":
        return None
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    match = re.search(r'^BUILD_VERSION = "([^"]*)"', text, re.M)
    raw = (match.group(1) if match else "dev").lstrip("vV")
    numbers = [int(part) for part in raw.split(".") if part.isdigit()] or [0]
    quad = tuple((numbers + [0, 0, 0, 0])[:4])
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo, StringFileInfo, StringStruct, StringTable,
            VarFileInfo, VarStruct, VSVersionInfo,
        )
    except ImportError as exc:
        # 版本资源只是署名与溯源信息，不能因为它拖垮发布构建。
        print(f"[spec] 跳过 exe 版本资源：{exc}")
        return None
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=quad, prodvers=quad, mask=0x3F, flags=0x0,
            OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0),
        ),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", AUTHOR),
                StringStruct("FileDescription", "BiliFavOrganizer — B站收藏夹整理工具"),
                StringStruct("FileVersion", raw),
                StringStruct("InternalName", "BiliFavOrganizer"),
                StringStruct("LegalCopyright", COPYRIGHT),
                StringStruct("OriginalFilename", "BiliFavOrganizer.exe"),
                StringStruct("ProductName", "BiliFavOrganizer"),
                StringStruct("ProductVersion", raw),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


VERSION_INFO = _release_metadata()

# These packages load some implementation modules dynamically at runtime.
for package in ("browsercookie", "qrcode", "PIL"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports
hiddenimports += collect_submodules("Cryptodome.Cipher")

a = Analysis(
    [str(ROOT / "server.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliFavOrganizer",
    debug=False,
    icon=str(APP_ICON),
    version=VERSION_INFO,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
collect = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BiliFavOrganizer",
)
