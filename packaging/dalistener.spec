from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


root = Path(SPECPATH).parent
datas = collect_data_files("dalistener")
binaries = []
hiddenimports = []

for package in ("fastapi", "uvicorn", "websockets", "openai", "keyring", "moonshine_voice", "faster_whisper", "ctranslate2", "onnxruntime"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

datas += [(str(root / "frontend" / "dist"), "frontend")]

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
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
app = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="DaListener",
)
