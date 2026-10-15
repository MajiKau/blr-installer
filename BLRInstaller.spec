from pathlib import Path
import sys
project_directory = Path(SPECPATH)

a = Analysis(
    [str(project_directory / "installer.py")],
    pathex=[str(project_directory)],
    binaries=[],
    datas=[
        (str(project_directory / "config.json"), "."),
        (str(project_directory / "icon.ico"), "."),
        (str(project_directory / "icon.png"), ".")
    ],
    hiddenimports=['PIL._tkinter_finder'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BLReviveInstallerV1_" + ('Windows' if sys.platform == 'win32' else 'Linux'),
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
    icon="icon.ico"
)
