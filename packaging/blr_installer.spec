from pathlib import Path
import sys
project_directory = Path(SPECPATH).parent
datas = [
    (str(project_directory / "config.json"), "."),
    (str(project_directory / "assets" / "icon.ico"), "assets"),
    (str(project_directory / "assets" / "icon.png"), "assets"),
]
if sys.platform == "win32":
    datas.extend(
        [
            (
                str(project_directory / "assets" / "directx_Jun2010_redist.exe"),
                "assets",
            ),
            (
                str(project_directory / "assets" / "vcredist_2010_x86.exe"),
                "assets",
            ),
            (
                str(project_directory / "assets" / "vcredist_2012_x86.exe"),
                "assets",
            ),
        ]
    )

a = Analysis(
    [str(project_directory / "installer.py")],
    pathex=[str(project_directory)],
    binaries=[],
    datas=datas,
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
    name="BLReviveInstaller_" + ('Windows' if sys.platform == 'win32' else 'Linux') + "_V1_0_0",
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
    icon=str(project_directory / "assets" / "icon.ico")
)
