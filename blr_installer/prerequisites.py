from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PrerequisiteResult:
    name: str
    status: str
    reboot_required: bool = False


class PrerequisiteInstallError(RuntimeError):
    pass


DIRECTX_NAME = "DirectX June 2010"
VC2010_NAME = "Microsoft Visual C++ 2010 (x86)"
VC2012_NAME = "Microsoft Visual C++ 2012 (x86)"

PREREQUISITE_DLLS = {
    DIRECTX_NAME: ("d3dx9_43.dll",),
    VC2010_NAME: ("MSVCP100.dll", "MSVCR100.dll"),
    VC2012_NAME: ("MSVCP110.dll", "MSVCR110.dll"),
}


def prerequisite_assets_directory() -> Path:
    if getattr(sys, "frozen", False):
        bundle_directory = Path(
            getattr(sys, "_MEIPASS", Path(sys.executable).parent)
        )
        return bundle_directory / "assets"
    return Path(__file__).resolve().parents[1] / "assets"


def _x86_system_directory() -> Path:
    windows_directory = os.environ.get("WINDIR")
    if not windows_directory:
        raise RuntimeError("Could not locate the Windows directory from WINDIR.")
    windows_path = Path(windows_directory)
    wow64_directory = windows_path / "SysWOW64"
    return wow64_directory if wow64_directory.is_dir() else windows_path / "System32"


def missing_windows_prerequisites() -> list[str]:
    if sys.platform != "win32":
        return []
    system_directory = _x86_system_directory()
    return [
        name
        for name, dlls in PREREQUISITE_DLLS.items()
        if not all((system_directory / dll).is_file() for dll in dlls)
    ]


def _run_elevated(executable: Path, arguments: list[str]) -> int:
    if sys.platform != "win32":
        raise RuntimeError("Windows prerequisites can only be installed on Windows.")

    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = str(executable.parent)
    info.nShow = 1

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error_code = ctypes.get_last_error()
        raise ctypes.WinError(error_code)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        wait_result = kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        if wait_result != 0:
            error_code = ctypes.get_last_error()
            raise ctypes.WinError(error_code)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            raise ctypes.WinError(error_code)
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def _run_setup(
    name: str,
    executable: Path,
    arguments: list[str],
    already_installed_codes: tuple[int, ...] = (),
) -> PrerequisiteResult:
    try:
        exit_code = _run_elevated(executable, arguments)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1223:
            raise PrerequisiteInstallError(
                f"{name} setup was cancelled at the Windows elevation prompt."
            ) from exc
        raise PrerequisiteInstallError(f"Could not start {name} setup: {exc}") from exc

    if exit_code == 0:
        return PrerequisiteResult(name, "Installed or already up to date.")
    if exit_code in already_installed_codes:
        return PrerequisiteResult(name, "A different version is already installed.")
    if exit_code in {1641, 3010}:
        return PrerequisiteResult(
            name,
            "Installed; Windows needs to restart to finish setup.",
            reboot_required=True,
        )
    raise PrerequisiteInstallError(
        f"{name} setup failed with exit code {exit_code}."
    )


def _extract_directx(redist: Path, destination: Path) -> Path:
    try:
        result = subprocess.run(
            [str(redist), "/Q", f"/T:{destination}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PrerequisiteInstallError(
            f"Could not extract DirectX June 2010 setup: {exc}"
        ) from exc
    if result.returncode != 0:
        raise PrerequisiteInstallError(
            "Could not extract DirectX June 2010 setup "
            f"(exit code {result.returncode})."
        )
    setup = destination / "DXSETUP.exe"
    if not setup.is_file():
        raise PrerequisiteInstallError(
            "DirectX June 2010 archive did not contain DXSETUP.exe."
        )
    return setup


def install_windows_prerequisites(
    missing: list[str],
    on_progress: Callable[[str], None] | None = None,
) -> list[PrerequisiteResult]:
    if sys.platform != "win32":
        raise RuntimeError("Windows prerequisites can only be installed on Windows.")
    unknown = set(missing) - PREREQUISITE_DLLS.keys()
    if unknown:
        raise ValueError(f"Unknown Windows prerequisite(s): {', '.join(sorted(unknown))}")
    if not missing:
        return []

    assets = prerequisite_assets_directory()
    directx_redist = assets / "directx_Jun2010_redist.exe"
    vc2010_redist = assets / "vcredist_2010_x86.exe"
    vc2012_redist = assets / "vcredist_2012_x86.exe"
    installers = {
        DIRECTX_NAME: directx_redist,
        VC2010_NAME: vc2010_redist,
        VC2012_NAME: vc2012_redist,
    }
    for name in missing:
        installer = installers[name]
        if not installer.is_file():
            raise FileNotFoundError(f"Bundled prerequisite installer not found: {installer}")

    results: list[PrerequisiteResult] = []
    for name in missing:
        if on_progress is not None:
            on_progress(f"Installing {name}...")
        if name == DIRECTX_NAME:
            with tempfile.TemporaryDirectory(prefix="blr-directx-") as temp_dir:
                directx_setup = _extract_directx(directx_redist, Path(temp_dir))
                results.append(
                    _run_setup(name, directx_setup, ["/silent"])
                )
        elif name == VC2010_NAME:
            results.append(
                _run_setup(name, vc2010_redist, ["/q", "/norestart"], (1638,))
            )
        else:
            results.append(
                _run_setup(
                    name,
                    vc2012_redist,
                    ["/install", "/quiet", "/norestart"],
                    (1638,),
                )
            )

    return results
