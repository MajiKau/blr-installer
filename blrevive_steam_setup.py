#!/usr/bin/env python3
"""
Configure a BLRevive non-Steam shortcut in Steam.

Requirements:
    python3 -m pip install vdf

Example:
    python3 blrevive_steam_setup.py \
        --exe "$HOME/Games/BLRevive/BlacklightRetribution.exe" \
        --game-app-id 480

Run only after fully exiting Steam. The script refuses to write Steam config
when it detects a running Steam process. Review the paths and backups before
using on a valuable Steam configuration.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, Callable

try:
    import vdf
except ImportError:
    vdf = None

from installer_errors import InstallationPaused


APP_NAME = "BLRevive"
PROTON_TOOL_NAME = "proton_experimental"
STEAM_ID64_BASE = 76561197960265728
LAUNCH_OPTIONS = (
    "-zcureurl=blrrevive.ddd-game.de "
    "-zcureport=80 "
    "-presenceurl=blrrevive.ddd-game.de "
    "-presenceport=9004"
)

SHORTCUT_DEFAULTS: dict[str, Any] = {
    "icon": "",
    "ShortcutPath": "",
    "LaunchOptions": "",
    "IsHidden": 0,
    "AllowDesktopConfig": 1,
    "AllowOverlay": 1,
    "OpenVR": 0,
    "Devkit": 0,
    "DevkitGameID": "",
    "DevkitOverrideAppID": 0,
    "LastPlayTime": 0,
    "FlatpakAppID": "",
    "tags": {},
}


def _print(*args: object, **kwargs: Any) -> None:
    if sys.stdout is not None:
        print(*args, **kwargs)


def steam_roots() -> list[Path]:
    """Common Steam roots, deduplicated by resolved path."""
    home = Path.home()
    if sys.platform == "win32":
        candidates: list[Path] = []
        try:
            import winreg

            for hive, key_name in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        for value_name in ("SteamPath", "InstallPath"):
                            try:
                                install_path, _ = winreg.QueryValueEx(key, value_name)
                            except OSError:
                                continue
                            candidates.append(Path(os.path.expandvars(install_path)))
                except OSError:
                    continue
        except ImportError:
            pass
        candidates.extend(
            Path(os.environ[env]) / "Steam"
            for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
            if os.environ.get(env)
        )
        candidates.extend((home / "AppData" / "Local" / "Steam", home / "Steam"))
    else:
        candidates = [
            home / ".steam" / "steam",
            home / ".steam" / "root",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        ]
    found: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if not resolved.is_dir():
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            found.append(resolved)
    return found


def steam_appears_running() -> bool:
    """Check whether Steam is running before editing files it may overwrite."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/FO", "CSV", "/NH"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Could not check whether Steam is running: {exc}") from exc
        return any(
            row and row[0].lower() == "steam.exe"
            for row in csv.reader(io.StringIO(result.stdout))
        )

    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(errors="ignore").strip().lower()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                errors="ignore"
            ).lower()
        except (OSError, PermissionError):
            continue
        if comm in {"steam", "steam.exe"}:
            return True
        # Catches common Flatpak launch processes. Avoid matching this script itself.
        if "com.valvesoftware.steam" in cmdline and "blrevive_steam_setup.py" not in cmdline:
            return True
    return False


def ensure_steam_closed(
    on_prompt: Callable[[], bool] | None = None,
) -> None:
    """Wait for Steam to close, prompting only when shortcut setup is imminent."""
    if not steam_appears_running():
        return

    if on_prompt is None:
        _print(
            "Steam must be fully closed before adding the library shortcut. "
            "Close Steam, including its tray process."
        )
        while True:
            answer = input(
                "Press Enter to check again, or type 'c' to pause installation: "
            ).strip().lower()
            if answer == "c":
                raise InstallationPaused
            if not steam_appears_running():
                return
            _print("Steam is still running. Exit it completely and try again.")

    while True:
        if not on_prompt():
            raise InstallationPaused
        if not steam_appears_running():
            return


def _require_vdf() -> Any:
    if vdf is None:
        raise RuntimeError(
            "The VDF package is required to configure Steam. "
            "Install the installer's dependencies and try again."
        )
    return vdf


def choose_steam_root(roots: list[Path], requested: str | None) -> Path:
    if requested:
        candidate = Path(requested).expanduser().resolve()
        if not candidate.is_dir():
            raise RuntimeError(f"Steam root does not exist: {candidate}")
        return candidate
    if not roots:
        raise RuntimeError(
            "Could not find a standard Steam installation. "
            "Specify --steam-root PATH for a nonstandard installation."
        )
    if len(roots) == 1:
        return roots[0]

    print("Found multiple Steam roots:")
    for i, root in enumerate(roots, 1):
        print(f"  {i}. {root}")
    while True:
        raw = input("Choose the Steam root number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(roots):
            return roots[int(raw) - 1]


def choose_user_dir(steam_root: Path, requested: str | None) -> Path:
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        raise RuntimeError(f"Steam userdata directory not found: {userdata}")

    users = steam_userdata_dirs(steam_root)
    if requested:
        selected = userdata / requested
        if not selected.is_dir():
            raise RuntimeError(f"Steam user directory not found: {selected}")
        return selected
    if not users:
        raise RuntimeError(f"No numeric Steam user directories found in {userdata}")
    if len(users) == 1:
        return users[0]

    labels = steam_user_labels(steam_root, users)
    print("Found multiple Steam user directories:")
    for i, user in enumerate(users, 1):
        print(f"  {i}. {labels[user]}")
    while True:
        raw = input("Choose the Steam user number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(users):
            return users[int(raw) - 1]


def steam_userdata_dirs(steam_root: Path) -> list[Path]:
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        return []
    return sorted(
        (
            path
            for path in userdata.iterdir()
            if path.is_dir() and path.name.isdigit() and int(path.name) != 0
        ),
        key=lambda path: path.name,
    )


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.blrevive-backup-{stamp}")
    # Avoid overwriting a backup if two runs occur within the same second.
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.blrevive-backup-{stamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    return backup


def atomic_write(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fp:
            writer(fp)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def read_binary_shortcuts(path: Path) -> dict[str, Any]:
    vdf_module = _require_vdf()
    if not path.exists() or path.stat().st_size == 0:
        return {"shortcuts": {}}
    with path.open("rb") as fp:
        data = vdf_module.binary_load(fp)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected binary VDF structure in {path}")
    shortcuts = data.get("shortcuts")
    if shortcuts is None:
        data["shortcuts"] = {}
    elif not isinstance(shortcuts, dict):
        raise RuntimeError(f"Invalid 'shortcuts' object in {path}")
    return data


def generated_shortcut_appid(exe_quoted: str, name: str) -> int:
    """Steam-style 32-bit shortcut ID: CRC32 of quoted executable + name."""
    crc = zlib.crc32(f"{exe_quoted}{name}".encode("utf-8")) & 0xFFFFFFFF
    return crc | 0x80000000


def signed_shortcut_appid(shortcut_id: int) -> int:
    """Represent Steam's unsigned shortcut ID as the signed int32 used by binary VDF."""
    return shortcut_id - 0x100000000 if shortcut_id >= 0x80000000 else shortcut_id


def quoted_path(path: Path) -> str:
    # Steam's shortcut fields conventionally store quoted paths.
    # Reject quotes to avoid malformed VDF values.
    text = str(path)
    if '"' in text or "\n" in text or "\r" in text:
        raise RuntimeError(f'Path contains a quote or newline and cannot be stored safely: {text}')
    return f'"{text}"'


def find_blrevive_entry(shortcuts: dict[str, Any], exe_path: Path) -> tuple[str | None, dict[str, Any] | None]:
    """Prefer an exact executable match; otherwise match our reserved display name."""
    exe_quoted = quoted_path(exe_path)
    matches: list[tuple[str, dict[str, Any]]] = []
    for key, entry in shortcuts.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("Exe") == exe_quoted:
            matches.append((key, entry))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError("Multiple shortcuts already point to this executable; refusing to guess.")

    named = [
        (key, entry)
        for key, entry in shortcuts.items()
        if isinstance(entry, dict) and entry.get("AppName") == APP_NAME
    ]
    if len(named) > 1:
        raise RuntimeError(
            f"Found multiple shortcuts named {APP_NAME!r}; rename/remove duplicates in Steam first."
        )
    if named:
        # Update the existing BLRevive shortcut instead of creating a duplicate.
        return named[0]
    return None, None


def next_shortcut_key(shortcuts: dict[str, Any]) -> str:
    numeric = [int(k) for k in shortcuts if str(k).isdigit()]
    return str(max(numeric, default=-1) + 1)


def write_shortcut(shortcuts_file: Path, exe_path: Path, game_app_id: str, launch_options: str) -> int:
    data = read_binary_shortcuts(shortcuts_file)
    shortcuts: dict[str, Any] = data["shortcuts"]
    exe_value = quoted_path(exe_path)
    start_dir_value = quoted_path(exe_path.parent)
    key, entry = find_blrevive_entry(shortcuts, exe_path)

    if entry is None:
        key = next_shortcut_key(shortcuts)
        entry = dict(SHORTCUT_DEFAULTS)
        entry["tags"] = {}
        shortcuts[key] = entry

    shortcut_id = generated_shortcut_appid(exe_value, APP_NAME)
    # Update only fields managed by this installer; preserve unrelated fields/tags.
    entry.update({
        "appid": signed_shortcut_appid(shortcut_id),
        "AppName": APP_NAME,
        "Exe": exe_value,
        "StartDir": start_dir_value,
        "LaunchOptions": launch_options,
        "icon": str(exe_path),
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "FlatpakAppID": "",
    })
    if not isinstance(entry.get("tags"), dict):
        entry["tags"] = {}

    backup = backup_file(shortcuts_file)
    if backup:
        _print(f"Backed up shortcuts: {backup}")

    def writer(fp):
        _require_vdf().binary_dump(data, fp)

    atomic_write(shortcuts_file, writer)
    _print(f"Updated Steam shortcut: {APP_NAME} (shortcut app ID {shortcut_id})")
    _print(f"Game steam_appid.txt value selected: {game_app_id}")
    return shortcut_id


def load_text_vdf(path: Path) -> dict[str, Any]:
    vdf_module = _require_vdf()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", errors="strict") as fp:
        data = vdf_module.load(fp)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected VDF structure in {path}")
    return data


def set_proton_mapping(config_file: Path, shortcut_id: int) -> None:
    data = load_text_vdf(config_file)

    install_store = data.setdefault("InstallConfigStore", {})
    if not isinstance(install_store, dict):
        raise RuntimeError("Unexpected InstallConfigStore structure in config.vdf")
    software = install_store.setdefault("Software", {})
    valve = software.setdefault("Valve", {})
    steam = valve.setdefault("Steam", {})
    mapping = steam.setdefault("CompatToolMapping", {})
    if not isinstance(mapping, dict):
        raise RuntimeError("Unexpected CompatToolMapping structure in config.vdf")

    key = str(shortcut_id)
    previous = mapping.get(key)
    if previous is not None and not isinstance(previous, dict):
        raise RuntimeError(f"Unexpected Proton mapping for shortcut {key}")

    # Change only this shortcut's mapping; preserve every other mapping.
    mapping[key] = {
        "name": PROTON_TOOL_NAME,
        "config": "",
        "priority": "250",
    }

    backup = backup_file(config_file)
    if backup:
        _print(f"Backed up Steam config: {backup}")

    def writer(fp):
        # Steam's config.vdf is text VDF, unlike binary shortcuts.vdf.
        text = _require_vdf().dumps(data, pretty=True)
        fp.write(text.encode("utf-8"))

    atomic_write(config_file, writer)
    _print(f"Set compatibility tool for shortcut {shortcut_id}: Proton Experimental")


def write_game_appid(exe_path: Path, game_app_id: str) -> None:
    appid_file = exe_path.parent / "steam_appid.txt"
    if appid_file.exists():
        current = appid_file.read_text(encoding="ascii", errors="replace").strip()
        if current == game_app_id:
            _print(f"steam_appid.txt already contains {game_app_id}")
            return
        backup = backup_file(appid_file)
        if backup:
            _print(f"Backed up existing app ID file: {backup}")
    else:
        _print(f"Creating {appid_file}")

    payload = f"{game_app_id}\n".encode("ascii")
    atomic_write(appid_file, lambda fp: fp.write(payload))
    _print(f"Wrote {appid_file}: {game_app_id}")


def _automatic_steam_root() -> Path:
    roots = steam_roots()
    if not roots:
        raise RuntimeError(
            "Could not find a Steam installation. Start Steam once and try again."
        )
    return roots[0]


def _automatic_user_dir(
    steam_root: Path,
    on_choose_user: Callable[[list[tuple[Path, str]]], Path | None] | None = None,
) -> Path:
    userdata = steam_root / "userdata"
    users = steam_userdata_dirs(steam_root)
    if not users:
        raise RuntimeError(f"No numeric Steam user directories found in {userdata}")
    if len(users) == 1:
        return users[0]

    choices = [(user, label) for user, label in steam_user_labels(steam_root, users).items()]
    selected = (
        on_choose_user(choices)
        if on_choose_user is not None
        else choose_user_dir(steam_root, requested=None)
    )
    if selected is None:
        raise InstallationPaused
    selected = selected.expanduser().resolve()
    if selected not in users:
        raise ValueError("The selected Steam user directory is not valid.")
    return selected


def steam_user_labels(steam_root: Path, users: list[Path]) -> dict[Path, str]:
    """Resolve human-readable display/account names from Steam's loginusers.vdf."""
    user_data = load_text_vdf(steam_root / "config" / "loginusers.vdf").get("users", {})
    labels: dict[Path, str] = {}
    account_names: dict[Path, str] = {}
    for user in users:
        record: object = {}
        if isinstance(user_data, dict):
            try:
                steam_id64 = str(STEAM_ID64_BASE + int(user.name))
            except ValueError:
                steam_id64 = ""
            record = user_data.get(steam_id64, user_data.get(user.name, {}))
        if not isinstance(record, dict):
            record = {}
        display_name = str(record.get("PersonaName", "")).strip()
        account_name = str(record.get("AccountName", "")).strip()
        account_names[user] = account_name
        if display_name and account_name and display_name.casefold() != account_name.casefold():
            labels[user] = f"{display_name} (@{account_name})"
        else:
            labels[user] = display_name or account_name or "Steam account name unavailable"

    duplicate_labels = {
        label for label, count in Counter(labels.values()).items() if count > 1
    }
    for user, label in labels.items():
        if label in duplicate_labels and account_names[user] and f"@{account_names[user]}" not in label:
            labels[user] = f"{label} (@{account_names[user]})"
    duplicate_labels = {
        label for label, count in Counter(labels.values()).items() if count > 1
    }
    for user, label in labels.items():
        if label in duplicate_labels or label == "Steam account name unavailable":
            labels[user] = f"{labels[user]} (user ID {user.name})"
    return labels


def steam_game_id(shortcut_appid: int) -> int:
    """Encode a shortcut app ID as the 64-bit GameID expected by Steam URLs."""
    return (shortcut_appid << 32) | 0x02000000


def configure_steam_shortcut(
    exe_path: Path,
    game_app_id: str,
    launch_options: str = LAUNCH_OPTIONS,
    on_steam_prompt: Callable[[], bool] | None = None,
    on_choose_user: Callable[[list[tuple[Path, str]]], Path | None] | None = None,
) -> tuple[int, Path]:
    """Write the BLRevive shortcut and return its generated ID and Steam root."""
    if game_app_id not in {"480", "209870"}:
        raise ValueError("Choose a valid Steam ownership option.")
    exe_path = exe_path.expanduser().resolve()
    if not exe_path.is_file():
        raise RuntimeError(f"The game executable was not found: {exe_path}")
    if exe_path.suffix.lower() != ".exe":
        raise RuntimeError(f"Expected a Windows .exe for Proton, got: {exe_path}")
    ensure_steam_closed(on_steam_prompt)

    root = _automatic_steam_root()
    user_dir = _automatic_user_dir(root, on_choose_user)
    config_file = root / "config" / "config.vdf"
    if not config_file.is_file():
        raise RuntimeError(
            f"Steam config file not found: {config_file}. Start Steam once and retry."
        )
    shortcut_id = write_shortcut(
        user_dir / "config" / "shortcuts.vdf",
        exe_path,
        game_app_id,
        launch_options,
    )
    if sys.platform.startswith("linux"):
        set_proton_mapping(config_file, shortcut_id)
    write_game_appid(exe_path, game_app_id)
    return shortcut_id, root


def launch_steam(steam_root: Path, shortcut_id: int | None = None) -> None:
    """Launch Steam, optionally opening the BLRevive shortcut directly."""
    uri_args = (
        [f"steam://rungameid/{shortcut_id}"] if shortcut_id is not None else []
    )
    if sys.platform == "win32":
        steam_exe = steam_root / "steam.exe"
        if not steam_exe.is_file():
            raise RuntimeError(f"Steam executable not found: {steam_exe}")
        subprocess.Popen([str(steam_exe), *uri_args], cwd=steam_root)
    elif "com.valvesoftware.Steam" in str(steam_root):
        flatpak = shutil.which("flatpak")
        if flatpak is None:
            raise RuntimeError("Flatpak is required to launch this Steam installation.")
        subprocess.Popen([flatpak, "run", "com.valvesoftware.Steam", *uri_args])
    else:
        steam = steam_root / "steam"
        if not steam.is_file():
            located_steam = shutil.which("steam")
            if located_steam is None:
                raise RuntimeError(f"Steam launcher not found in {steam_root}")
            steam = Path(located_steam)
        subprocess.Popen([str(steam), *uri_args], cwd=steam_root)


def launch_steam_shortcut(exe_path: Path) -> None:
    """Open the Steam library shortcut associated with this executable."""
    exe_value = quoted_path(exe_path.expanduser().resolve())
    shortcut_id = generated_shortcut_appid(exe_value, APP_NAME)
    launch_steam(_automatic_steam_root(), steam_game_id(shortcut_id))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add BLRevive to Linux Steam and force Proton Experimental."
    )
    parser.add_argument(
        "--exe", required=True, type=Path,
        help="Absolute path to the BLRevive Windows .exe inside the installed game directory.",
    )
    parser.add_argument(
        "--game-app-id", required=True, choices=("480", "209870"),
        help="Value to write to the game's steam_appid.txt (choose the known-good value).",
    )
    parser.add_argument("--steam-root", help="Explicit Steam root, for nonstandard installations.")
    parser.add_argument("--user-id", help="Numeric Steam userdata directory name.")
    parser.add_argument(
        "--launch-options", default=LAUNCH_OPTIONS,
        help="Override the Steam shortcut launch options.",
    )
    args = parser.parse_args()

    exe_path = args.exe.expanduser().resolve()
    if not exe_path.is_file():
        parser.error(f"Executable does not exist: {exe_path}")
    if exe_path.suffix.lower() != ".exe":
        parser.error(f"Expected a Windows .exe for Proton, got: {exe_path}")

    if steam_appears_running():
        print(
            "Steam appears to be running. Exit Steam completely (including its tray process), "
            "then run this script again. Steam can overwrite external shortcut edits.",
            file=sys.stderr,
        )
        return 2

    roots = steam_roots()
    root = choose_steam_root(roots, args.steam_root)
    user_dir = choose_user_dir(root, args.user_id)
    config_dir = user_dir / "config"
    shortcuts_file = config_dir / "shortcuts.vdf"
    config_file = root / "config" / "config.vdf"
    if not config_file.is_file():
        raise RuntimeError(
            f"Steam config file not found: {config_file}. "
            "Start Steam once, exit it completely, and retry."
        )

    print(f"Steam root: {root}")
    print(f"Steam user: {user_dir.name}")
    print(f"Executable: {exe_path}")
    print(f"Launch options: {args.launch_options}")

    shortcut_id = write_shortcut(
        shortcuts_file, exe_path, args.game_app_id, args.launch_options
    )
    if sys.platform.startswith("linux"):
        set_proton_mapping(config_file, shortcut_id)
    write_game_appid(exe_path, args.game_app_id)

    print("\nSetup written. Start Steam and check the BLRevive library entry.")
    print("Confirm that Compatibility shows Proton Experimental and test server connectivity.")
    print("This script does not install Proton Experimental if it is missing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
