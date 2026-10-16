from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from .config import STEAM_OPTIONS


def state_directory() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "BLRevive" / "installer"


def default_destination() -> Path:
    if sys.platform == "win32":
        local_app_data_value = os.environ.get("LOCALAPPDATA")
        local_app_data = (
            Path(local_app_data_value)
            if local_app_data_value
            else Path.home() / "AppData" / "Local"
        )
        return local_app_data / "BLRevive"
    return Path.home() / "Games"


def atomic_write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(data), encoding="utf-8")
    temporary_path.replace(path)


def read_job_state(path: Path, url: str) -> dict[str, object] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read saved installation progress: {exc}") from exc

    expected_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    if not isinstance(state, dict) or state.get("url_hash") != expected_hash:
        raise ValueError(f"Saved installation progress is invalid: {path}")
    if not isinstance(state.get("destination"), str):
        raise ValueError(f"Saved installation settings are invalid: {path}")
    steam_app_id = state.get("steam_app_id")
    if isinstance(steam_app_id, int) and not isinstance(steam_app_id, bool):
        steam_app_id = str(steam_app_id)
        state["steam_app_id"] = steam_app_id
    if steam_app_id not in {value for _, value in STEAM_OPTIONS.values()}:
        raise ValueError(f"Saved Steam app ID is invalid: {path}")
    if not isinstance(state.get("completed_members", []), list):
        raise ValueError(f"Saved extraction progress is invalid: {path}")
    if state.get("phase") not in {"downloading", "extracting", "complete"}:
        raise ValueError(f"Saved installation phase is invalid: {path}")
    return state


def remember_destination(destination: Path) -> None:
    atomic_write_json(
        state_directory() / "settings.json",
        {"last_destination": str(destination.expanduser().resolve())},
    )


def last_destination() -> Path:
    settings_path = state_directory() / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_destination()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read saved installer settings: {exc}") from exc
    value = settings.get("last_destination") if isinstance(settings, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"Saved installer settings are invalid: {settings_path}")
    return Path(value)


def destination_key(destination: Path) -> str:
    return str(destination.expanduser().resolve())


def extraction_progress_for_destination(
    state: dict[str, object],
    destination: Path,
) -> dict[str, object]:
    progress_by_destination = state.get("extraction_progress")
    if not isinstance(progress_by_destination, dict):
        progress_by_destination = {}
        state["extraction_progress"] = progress_by_destination

    key = destination_key(destination)
    progress = progress_by_destination.get(key)
    if isinstance(progress, dict):
        return progress

    progress = {
        "completed_members": [],
        "current_member_index": None,
        "current_member_bytes": 0,
    }
    if state.get("destination") == key:
        for field in (
            "completed_members",
            "current_member_index",
            "current_member_bytes",
            "archive_signature",
        ):
            if field in state:
                progress[field] = state[field]
    progress_by_destination[key] = progress
    return progress


def reset_install_progress(state: dict[str, object], destination: Path) -> None:
    state["phase"] = "downloading"
    progress = extraction_progress_for_destination(state, destination)
    progress.update(
        {
            "completed_members": [],
            "current_member_index": None,
            "current_member_bytes": 0,
        }
    )
    progress.pop("archive_signature", None)
