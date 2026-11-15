# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 設定：Windows は .exe（フォルダ形式）、Mac は .app を作る
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

APP_NAME = "ShuppinPhoto"
DISPLAY_NAME = "出品写真ツール"
VERSION = "1.2.0"

datas, binaries, hiddenimports = [], [], []
for pkg in ("tkinterdnd2", "onnxruntime"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
hiddenimports += collect_submodules("scipy.ndimage")
hiddenimports += ["pillow_heif", "shuppin_photo", "orientation", "PIL.ImageTk"]

datas += [("assets/icon.png", "assets")]
if Path("models/isnet-general-use.onnx").exists():
    datas += [("models/isnet-general-use.onnx", "models")]

a = Analysis(
    ["shuppin_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["rembg", "pymatting", "numba", "llvmlite", "skimage", "pooch", "matplotlib", "IPython", "jupyter", "pytest", "PyQt5", "PyQt6", "PySide6", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon="assets/icon.ico" if sys.platform.startswith("win") else "assets/icon.icns",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon="assets/icon.icns",
        bundle_identifier="jp.shuppinphoto.app",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": DISPLAY_NAME,
            "CFBundleName": DISPLAY_NAME,
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
