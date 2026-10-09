from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
STEAM_OPTIONS = {
    "1": ("Own on Steam", "209870"),
    "2": ("Don't own on Steam", "480"),
}
TEXT_FILE = Path("blacklightretribution") / "Binaries" / "Win32" / "steam_appid.txt"
GAME_EXECUTABLE = Path("blacklightretribution") / "Binaries" / "Win32" / "FoxGame-win32-Shipping.exe"
DEFAULT_LAUNCH_ARGS = (
    "-zcureurl=blrrevive.ddd-game.de",
    "-zcureport=80",
    "-presenceurl=blrrevive.ddd-game.de",
    "-presenceport=9004",
)
if getattr(sys, "frozen", False):
    _bundle_directory = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    _external_config = Path(sys.executable).resolve().parent / "config.json"
    CONFIG_FILE = _external_config if _external_config.is_file() else _bundle_directory / "config.json"
else:
    CONFIG_FILE = Path(__file__).with_name("config.json")


def load_config() -> dict[str, object]:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {CONFIG_FILE.name}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{CONFIG_FILE.name} must contain a JSON object.")
    return config


def load_zip_url() -> str:
    config = load_config()
    url = config.get("zip_url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(
            f"Set a ZIP download URL in the 'zip_url' field of {CONFIG_FILE.name}."
        )

    parsed_url = urllib.parse.urlparse(url.strip())
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("The configured ZIP URL must start with http:// or https://.")
    return url.strip()


def load_launch_args() -> tuple[str, ...]:
    config = load_config()
    launch_args = config.get("launch_args", list(DEFAULT_LAUNCH_ARGS))
    if (
        not isinstance(launch_args, list)
        or any(not isinstance(argument, str) or not argument for argument in launch_args)
    ):
        raise ValueError(
            f"The 'launch_args' field in {CONFIG_FILE.name} must be a list of non-empty strings."
        )
    return tuple(launch_args)
