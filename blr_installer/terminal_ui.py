from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Callable

from .config import GAME_EXECUTABLE, STEAM_OPTIONS, TEXT_FILE
from .download import format_size, metadata_path, read_resume_metadata
from .state import extraction_progress_for_destination, last_destination
from .steam import launch_steam_shortcut


def choose_steam_option() -> str:
    print("Choose the Steam ownership option:")
    for key, (label, _) in STEAM_OPTIONS.items():
        print(f"  {key}. {label}")

    while True:
        choice = input("Enter 1 or 2: ").strip()
        if choice in STEAM_OPTIONS:
            return STEAM_OPTIONS[choice][1]
        print("Please enter 1 or 2.")


def choose_destination(default: Path | None = None) -> Path:
    default = default or last_destination()
    entered_path = input(f"Install folder [{default}]: ").strip()
    chosen = Path(entered_path).expanduser() if entered_path else default
    return chosen.resolve()


def show_saved_progress(
    state: dict[str, object],
    download_path: Path,
    url: str,
) -> None:
    phase = state.get("phase")
    if phase == "downloading":
        current = download_path.stat().st_size if download_path.is_file() else 0
        metadata = read_resume_metadata(
            metadata_path(download_path),
            url,
            download_path,
        )
        total_value = metadata.get("total_bytes")
        if total_value is not None:
            try:
                total = int(total_value)
            except ValueError:
                total = 0
            if total > 0:
                print(
                    f"Saved progress: download {format_size(current)} of "
                    f"{format_size(total)} ({min(current / total, 1):.0%})."
                )
                return
        print(f"Saved progress: {format_size(current)} downloaded.")
        return

    if phase == "extracting" and download_path.is_file():
        destination = Path(str(state["destination"])).expanduser().resolve()
        progress = extraction_progress_for_destination(state, destination)
        completed_value = progress.get("completed_members", [])
        try:
            with zipfile.ZipFile(download_path) as archive:
                members = archive.infolist()
                total = sum(member.file_size for member in members if not member.is_dir())
                completed = {
                    value
                    for value in completed_value
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value < len(members)
                } if isinstance(completed_value, list) else set()
                current_index = progress.get("current_member_index")
                current_bytes = progress.get("current_member_bytes", 0)
                current = sum(members[index].file_size for index in completed)
                if (
                    isinstance(current_index, int)
                    and not isinstance(current_index, bool)
                    and 0 <= current_index < len(members)
                    and isinstance(current_bytes, int)
                    and not isinstance(current_bytes, bool)
                ):
                    current += min(current_bytes, members[current_index].file_size)
                print(
                    f"Saved progress: extraction {format_size(current)} of "
                    f"{format_size(total)} "
                    f"({1 if total == 0 else min(current / total, 1):.0%})."
                )
                return
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"Saved extraction progress exists; details unavailable: {exc}")
            return
    print("Saved installation progress is available.")


def prompt_for_pending_action() -> str:
    while True:
        print("  1. Continue")
        print("  2. Change install settings")
        print("  3. Cancel and keep saved progress")
        choice = input("Choose an option [1]: ").strip() or "1"
        if choice in {"1", "2", "3"}:
            return choice
        print("Please enter 1, 2, or 3.")


def prompt_for_completed_action() -> str:
    while True:
        print("Installation is already complete.")
        print("  1. Launch the game")
        print("  2. Reinstall with the saved settings")
        print("  3. Change install settings and reinstall")
        print("  4. Exit")
        choice = input("Choose an option [1]: ").strip() or "1"
        if choice in {"1", "2", "3", "4"}:
            return choice
        print("Please enter 1, 2, 3, or 4.")


def prompt_keep_download() -> bool:
    while True:
        choice = input("Keep the downloaded ZIP for faster reinstallation? [y/N]: ").strip().lower()
        if choice in {"", "n", "no"}:
            return False
        if choice in {"y", "yes"}:
            return True
        print("Please enter y or n.")


def launch_game(
    destination: Path,
    steam_app_id: str,
    on_message: Callable[[str], None] | None = None,
) -> bool:
    executable = destination / GAME_EXECUTABLE
    if not executable.is_file():
        message = (
            f"The game executable was not found: {executable}. "
            "You can reinstall the game."
        )
        (on_message or print)(message)
        return False

    valid_app_ids = {app_id for _, app_id in STEAM_OPTIONS.values()}
    if steam_app_id not in valid_app_ids:
        raise ValueError("Choose a valid Steam ownership option.")

    steam_app_id_file = destination / TEXT_FILE
    steam_app_id_file.parent.mkdir(parents=True, exist_ok=True)
    steam_app_id_file.write_text(steam_app_id, encoding="utf-8")

    launch_steam_shortcut(executable)
    (on_message or print)("Launching BLRevive through Steam...")
    return True
