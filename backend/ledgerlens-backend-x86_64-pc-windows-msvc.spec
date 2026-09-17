# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, copy_metadata

datas = []
binaries = []
hiddenimports = ['sqlite_store', 'uvicorn', 'multipart', 'python_multipart']
tmp_ret = collect_all('engine')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('reportlab')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# PaddleOCR is imported dynamically by the local OCR adapter, so PyInstaller's
# static analysis cannot discover it. Collect its Python modules, package data,
# and native Paddle/PaddleX dependencies explicitly for the Windows sidecar.
for package in ('paddleocr', 'paddle', 'paddlex'):
    tmp_ret = collect_all(package)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# PaddleX determines OCR pipeline availability from distribution metadata.
# Preserve both the import packages and their .dist-info records in the frozen app.
ocr_core_packages = {
    'imagesize': 'imagesize',
    'opencv-contrib-python': 'cv2',
    'pyclipper': 'pyclipper',
    'pypdfium2': 'pypdfium2',
    'python-bidi': 'bidi',
    'shapely': 'shapely',
}
for distribution, package in ocr_core_packages.items():
    tmp_ret = collect_all(package)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
    datas += copy_metadata(distribution)


a = Analysis(
    ['server.py'],
    pathex=[],
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
    a.binaries,
    a.datas,
    [],
    name='ledgerlens-backend-x86_64-pc-windows-msvc',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
