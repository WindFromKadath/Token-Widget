# -*- mode: python ; coding: utf-8 -*-
# Token 挂件单 exe 打包（PyInstaller）。
#
# 构建：uv run pyinstaller --clean --noconfirm TokenWidget.spec
# 产物：dist/TokenWidget.exe（onefile；config.json 在 exe 同目录自动创建）
#
# 说明：
# - QtWebEngine（M3 重登 webview）由 pyinstaller-hooks-contrib 自动收集
#   QtWebEngineProcess.exe 与 .pak 资源
# - 排除未使用的 Qt 模块压缩体积（WebEngine 依赖的 QtQml/QtQuick/网络等保留）
# - 数据文件：CC Switch 图标 SVG + Node runner.js

datas = [
    ("token_widget/assets", "token_widget/assets"),
    ("token_widget/collectors/runner.js", "token_widget/collectors"),
]

# 与 QtWebEngine 无依赖关系的大块 Qt 模块，全部排除
excludes = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtQuick3D", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtTest",
    "PySide6.QtWebSockets", "PySide6.QtXml", "PySide6.QtXmlPatterns",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TokenWidget",
    icon="token_widget/assets/app.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
