from __future__ import annotations

import ctypes
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


def prerequisite_assets_directory() -> Path:
    if getattr(sys, "frozen", False):
        bundle_directory = Path(
            getattr(sys, "_MEIPASS", Path(sys.executable).parent)
        )
        return bundle_directory / "assets"
    return Path(__file__).resolve().parents[1] / "assets"


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
    on_progress: Callable[[str], None] | None = None,
) -> list[PrerequisiteResult]:
    if sys.platform != "win32":
        raise RuntimeError("Windows prerequisites can only be installed on Windows.")

    assets = prerequisite_assets_directory()
    directx_redist = assets / "directx_Jun2010_redist.exe"
    vc2010_redist = assets / "vcredist_2010_x86.exe"
    vc2012_redist = assets / "vcredist_2012_x86.exe"
    for installer in (directx_redist, vc2010_redist, vc2012_redist):
        if not installer.is_file():
            raise FileNotFoundError(f"Bundled prerequisite installer not found: {installer}")

    results: list[PrerequisiteResult] = []
    with tempfile.TemporaryDirectory(prefix="blr-directx-") as temp_dir:
        if on_progress is not None:
            on_progress("Installing DirectX June 2010...")
        directx_setup = _extract_directx(directx_redist, Path(temp_dir))
        results.append(
            _run_setup(
                "DirectX June 2010",
                directx_setup,
                ["/silent"],
            )
        )

    for name, installer, arguments, already_installed_codes in (
        (
            "Microsoft Visual C++ 2010 (x86)",
            vc2010_redist,
            ["/q", "/norestart"],
            (1638,),
        ),
        (
            "Microsoft Visual C++ 2012 (x86)",
            vc2012_redist,
            ["/install", "/quiet", "/norestart"],
            (1638,),
        ),
    ):
        if on_progress is not None:
            on_progress(f"Installing {name}...")
        results.append(_run_setup(name, installer, arguments, already_installed_codes))

    return results
