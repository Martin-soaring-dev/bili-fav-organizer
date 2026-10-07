# Build the self-contained Windows release package with:
#   pyinstaller --noconfirm --clean BiliFavOrganizer.spec
from pathlib import Path

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
