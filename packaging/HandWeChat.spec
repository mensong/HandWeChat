# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH).resolve().parent

common_datas = [
    (str(ROOT / "handwechat" / "ocr_win.ps1"), "handwechat"),
    (str(ROOT / "ocr_models"), "ocr_models"),
]
common_binaries = []
common_hiddenimports = ["handwechat.ocr_channel"]

for package_name in ("paddle", "paddleocr", "fastapi", "uvicorn", "websockets"):
    datas, binaries, hiddenimports = collect_all(package_name)
    common_datas.extend(datas)
    common_binaries.extend(binaries)
    common_hiddenimports.extend(hiddenimports)


api_datas = common_datas + [
    (str(ROOT / "handwechat" / "api" / "static"), "handwechat/api/static"),
]
api_a = Analysis(
    [str(ROOT / "scripts" / "run_api.py")],
    pathex=[str(ROOT)],
    binaries=common_binaries,
    datas=api_datas,
    hiddenimports=common_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
api_pyz = PYZ(api_a.pure)
api_exe = EXE(
    api_pyz,
    api_a.scripts,
    [],
    exclude_binaries=True,
    name="HandWeChatApi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


gui_a = Analysis(
    [str(ROOT / "scripts" / "run_gui.py")],
    pathex=[str(ROOT)],
    binaries=common_binaries,
    datas=common_datas,
    hiddenimports=common_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="HandWeChatGui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


coll = COLLECT(
    api_exe,
    gui_exe,
    api_a.binaries,
    api_a.datas,
    gui_a.binaries,
    gui_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="HandWeChat",
)
