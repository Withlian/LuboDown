# -*- mode: python ; coding: utf-8 -*-
"""LuboDown 打包脚本。

    pyinstaller --noconfirm LuboDown.spec

产物为单目录 dist/LuboDown/（LuboDown.exe + _internal/ + bin/ffmpeg.exe + ui 资源），
整个目录可便携移动；config/ 与 downloads/ 在首次运行时于 exe 同级创建。
"""
from pathlib import Path

ROOT = Path(SPECPATH)   # noqa: F821  —— SPECPATH 由 PyInstaller 注入
ICON = ROOT / "build" / "icon.ico"

a = Analysis(  # noqa: F821
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # 冻结态下 main.py 从 sys._MEIPASS/ui/index.html 载入前端，故目标目录名必须是 ui
    datas=[(str(ROOT / "app" / "ui"), "ui")],
    hiddenimports=[
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr",
        "clr_loader",
        "pythonnet",
        "qrcode",
        "qrcode.image.pil",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest", "pydoc"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LuboDown",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # pythonw 式无控制台；崩溃信息写 config/lubo.log
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LuboDown",
)
