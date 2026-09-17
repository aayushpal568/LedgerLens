# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for package in ('engine', 'paddleocr', 'paddle', 'paddlex'):
    d, b, h = collect_all(package)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(['ocr_packaged_diagnostic.py'], pathex=[], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='ocr-diagnostic', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=True, console=True,
          disable_windowed_traceback=False)
