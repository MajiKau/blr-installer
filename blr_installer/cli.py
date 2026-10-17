from __future__ import annotations

import argparse
import hashlib
import http.client
import sys
import urllib.error
import zipfile
from pathlib import Path

from .config import load_zip_url
from .download import cached_download_path, discard_download_cache
from .errors import InstallationPaused
from .service import run_install as _run_install
from .steam import launch_steam
from .state import (
    read_job_state as _read_job_state,
    remember_destination as _remember_destination,
    reset_install_progress as _reset_install_progress,
    state_directory as _state_directory,
)
from .terminal_ui import (
    choose_destination,
    choose_steam_option,
    launch_game as _launch_game,
    prompt_for_completed_action as _prompt_for_completed_action,
    prompt_keep_download as _prompt_keep_download,
    prompt_for_pending_action as _prompt_for_pending_action,
    prompt_install_prerequisites as _prompt_install_prerequisites,
    prompt_launch_steam as _prompt_launch_steam,
    show_saved_progress as _show_saved_progress,
)


def install() -> None:
    url = load_zip_url()
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    saved_state_directory = _state_directory()
    state_path = saved_state_directory / f"install-{url_hash}.json"
    download_path = saved_state_directory / f"download-{url_hash}.zip.part"
    state = _read_job_state(state_path, url)

    if state is not None and state.get("phase") == "complete":
        print(f"A completed installation was found at {state['destination']}.")
        action = _prompt_for_completed_action()
        destination = Path(str(state["destination"])).expanduser().resolve()
        if action == "4":
            return
        if action == "1":
            _launch_game(destination, str(state["steam_app_id"]))
            return
        if action == "3":
            state["steam_app_id"] = choose_steam_option()
            destination = choose_destination()
            state["destination"] = str(destination)
            _remember_destination(destination)
        _reset_install_progress(state, destination)
    elif state is not None:
        print(
            "An interrupted installation was found for "
            f"{state['destination']} ({state.get('phase', 'in progress')})."
        )
        _show_saved_progress(state, download_path, url)
        action = _prompt_for_pending_action()
        if action == "3":
            print("Installation cancelled; saved progress was kept.")
            return

        if action == "1":
            destination = Path(str(state["destination"])).expanduser().resolve()
            steam_app_id = str(state["steam_app_id"])
        else:
            steam_app_id = choose_steam_option()
            destination = choose_destination()
            state["destination"] = str(destination)
            state["steam_app_id"] = steam_app_id
        state["phase"] = (
            "extracting"
            if state.get("phase") == "extracting"
            and cached_download_path(download_path).is_file()
            else "downloading"
        )
        _remember_destination(destination)
    else:
        steam_app_id = choose_steam_option()
        destination = choose_destination()
        _remember_destination(destination)
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

    while True:
        steam_root = _run_install(
            url,
            state_path,
            download_path,
            state,
            on_prerequisites_prompt=_prompt_install_prerequisites,
        )
        if _prompt_launch_steam():
            launch_steam(steam_root)
        if _prompt_keep_download():
            print(f"Keeping downloaded ZIP at {cached_download_path(download_path)}")
        else:
            discard_download_cache(download_path)
            print("Downloaded ZIP removed.")
        action = _prompt_for_completed_action()
        destination = Path(str(state["destination"])).expanduser().resolve()
        if action == "4":
            return
        if action == "1":
            _launch_game(destination, str(state["steam_app_id"]))
            return
        if action == "3":
            state["steam_app_id"] = choose_steam_option()
            destination = choose_destination()
            state["destination"] = str(destination)
            _remember_destination(destination)
        _reset_install_progress(state, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Blacklight Retribution.")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="run the terminal installer instead of the graphical interface",
    )
    arguments = parser.parse_args()
    if not arguments.cli or getattr(sys, "frozen", False):
        from .gui import run_gui

        run_gui()
        return 0

    try:
        install()
    except KeyboardInterrupt:
        print(
            "\nInstallation paused. Progress was saved; run the installer again to continue.",
            file=sys.stderr,
        )
        return 130
    except (
        OSError,
        RuntimeError,
        ValueError,
        http.client.HTTPException,
        zipfile.BadZipFile,
        urllib.error.URLError,
        InstallationPaused,
    ) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
