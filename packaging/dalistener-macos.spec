import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


root = Path(SPECPATH).parent
datas = collect_data_files("dalistener")
binaries = []
hiddenimports = []

for package in ("fastapi", "uvicorn", "websockets", "openai", "keyring"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

datas += [(str(root / "frontend" / "dist"), "frontend")]
datas += [(str(root / "extension"), "extension")]

analysis = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["nvidia"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DaListener",
    console=False,
    target_arch="universal2",
    codesign_identity=os.environ.get("APPLE_CODESIGN_IDENTITY"),
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="DaListener",
)
app = BUNDLE(
    collection,
    name="DaListener.app",
    bundle_identifier="com.therealstubborndeveloper.dalistener",
    version="0.3.0a2",
    info_plist={
        "CFBundleDisplayName": "DaListener",
        "CFBundleShortVersionString": "0.3.0-alpha.2",
        "CFBundleVersion": "0.3.0.2",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    },
)
