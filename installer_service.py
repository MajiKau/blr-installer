from __future__ import annotations

import hashlib
import threading
import zipfile
from pathlib import Path
from typing import Callable

from blrevive_steam_setup import (
    configure_steam_shortcut,
    launch_steam,
)
from installer_config import (
    GAME_EXECUTABLE,
    STEAM_OPTIONS,
    TEXT_FILE,
    load_launch_args,
    load_zip_url,
)
from installer_download import download_zip
from installer_extract import safe_extract
from installer_progress import finish_progress, show_progress
from installer_state import (
    atomic_write_json,
    extraction_progress_for_destination,
    read_job_state,
    remember_destination,
    reset_install_progress,
    state_directory,
)


def run_install(
    url: str,
    state_path: Path,
    download_path: Path,
    state: dict[str, object],
    on_progress: Callable[[str, int, int | None], None] | None = None,
    pause_event: threading.Event | None = None,
    on_steam_prompt: Callable[[], bool] | None = None,
    on_choose_steam_user: Callable[[list[Path]], Path | None] | None = None,
) -> None:
    destination = Path(str(state["destination"])).expanduser().resolve()
    steam_app_id = str(state["steam_app_id"])
    extraction_progress = extraction_progress_for_destination(state, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_path, state)

    if state.get("phase") != "extracting" or not download_path.is_file():
        if on_progress is None:
            print("Downloading ZIP. Press Ctrl+C to pause and save progress.")
        state["phase"] = "downloading"
        atomic_write_json(state_path, state)
        download_zip(url, download_path, on_progress, pause_event)

    state["phase"] = "extracting"
    atomic_write_json(state_path, state)
    if on_progress is None:
        print(f"Extracting files to {destination}...")
    with zipfile.ZipFile(download_path) as archive:
        safe_extract(
            archive,
            destination,
            lambda current, total: (
                on_progress("Extract", current, total)
                if on_progress
                else show_progress("Extract", current, total)
            ),
            extraction_progress,
            lambda: atomic_write_json(state_path, state),
            pause_event,
        )
    if on_progress is None:
        finish_progress()
    text_file = destination / TEXT_FILE
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text(steam_app_id, encoding="utf-8")

    if on_progress is not None:
        on_progress("Steam setup", 0, None)
    _, steam_root = configure_steam_shortcut(
        destination / GAME_EXECUTABLE,
        steam_app_id,
        " ".join(load_launch_args()),
        on_steam_prompt,
        on_choose_steam_user,
    )

    state["phase"] = "complete"
    state["completed_members"] = []
    state["current_member_index"] = None
    state["current_member_bytes"] = 0
    atomic_write_json(state_path, state)
    launch_steam(steam_root)
    if on_progress is None:
        print(f"Installation complete. Created {text_file}")


def install_configured(
    destination: Path,
    steam_app_id: str,
    on_progress: Callable[[str, int, int | None], None],
    pause_event: threading.Event,
    on_steam_prompt: Callable[[], bool] | None = None,
    on_choose_steam_user: Callable[[list[Path]], Path | None] | None = None,
) -> None:
    if steam_app_id not in {value for _, value in STEAM_OPTIONS.values()}:
        raise ValueError("Choose a valid Steam ownership option.")
    url = load_zip_url()
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    saved_state_directory = state_directory()
    state_path = saved_state_directory / f"install-{url_hash}.json"
    download_path = saved_state_directory / f"download-{url_hash}.zip.part"
    state = read_job_state(state_path, url)
    destination = destination.expanduser().resolve()

    if state is None:
        state = {
            "url_hash": url_hash,
            "destination": str(destination),
            "steam_app_id": steam_app_id,
            "phase": "downloading",
            "completed_members": [],
            "current_member_index": None,
            "current_member_bytes": 0,
            "extraction_progress": {},
        }
    elif state.get("phase") == "complete":
        state["destination"] = str(destination)
        state["steam_app_id"] = steam_app_id
        reset_install_progress(state, destination)
    else:
        state["destination"] = str(destination)
        state["steam_app_id"] = steam_app_id
        if state.get("phase") == "extracting" and not download_path.is_file():
            state["phase"] = "downloading"
        elif state.get("phase") != "extracting":
            state["phase"] = "downloading"
    remember_destination(destination)
    run_install(
        url,
        state_path,
        download_path,
        state,
        on_progress,
        pause_event,
        on_steam_prompt,
        on_choose_steam_user,
    )
