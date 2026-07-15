from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def bundled_extension_source() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "extension"
    return Path(__file__).resolve().parents[2] / "extension"


def synchronize_browser_extension(destination: Path) -> Path:
    source = bundled_extension_source()
    if source.exists():
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination
