# Simple cross-platform installer

This interactive installer downloads a ZIP file, extracts it into the selected
folder, and writes the selected Steam app ID to
`blacklightretribution/Binaries/Win32/steam_appid.txt` inside that folder.

The terminal displays progress for downloading and extracting. Progress and
installation settings are saved in the user's application state directory
(`%LOCALAPPDATA%\blr-installer` on Windows or
`~/.local/state/blr-installer` on Linux, unless `XDG_STATE_HOME` is set).
The installer remembers the last selected install folder.
After an install finishes, it saves a completed status. It offers to launch the
game, reinstall with the saved settings, change settings and reinstall, or exit.
The same choices appear if you run the installer again later. Launch always
starts `blacklightretribution/Binaries/Win32/FoxGame-win32-Shipping.exe` with
the arguments in `config.json` and refreshes `steam_appid.txt` from the
currently selected Steam ownership option before starting it. On Linux, Wine
must be installed to launch this Windows executable.

If an install is interrupted, the next run shows the saved progress and lets you
continue, change the Steam choice or install folder, or cancel while keeping the
saved progress. Downloads resume when the server supports HTTP range requests;
otherwise, they restart from the beginning. During extraction, Ctrl+C pauses at
the current file chunk. Completed files and the current file offset are saved,
so extraction can resume without re-extracting completed data. Changing the
install folder resets extraction progress for that folder while retaining the
downloaded ZIP for resuming that install. Progress is shown as a percentage when the total size is known,
or as transferred bytes otherwise.
After installation, the installer asks whether to keep the downloaded ZIP for
faster reinstalls. If kept, a later reinstall reuses the cached archive; if
removed, the installer downloads it again.

## Configure

Edit `config.json` and set `zip_url` to the HTTPS or HTTP URL of the ZIP file:

```json
{
  "zip_url": "https://example.com/download.zip",
  "launch_args": [
    "-zcureurl=blrrevive.ddd-game.de",
    "-zcureport=80",
    "-presenceurl=blrrevive.ddd-game.de",
    "-presenceport=9004"
  ]
}
```

`launch_args` is an array of arguments passed directly to the game. Omit it to
use the defaults shown above, or set it to an empty array for no launch arguments.

## Run

Python 3 must be installed.

- **GUI:** `py installer.py` on Windows or `python3 installer.py` on Linux.
- **Terminal:** add `--cli` to either command.

The GUI allows choosing the Steam option and install folder, shows download and
extraction progress, and can pause/resume an installation. Closing the GUI while
an operation is active requests a pause and saves progress.

The terminal version prompts for whether you own the game on Steam and for an
install folder (press Enter to use the remembered location, initially
`installation` next to the installer). The generated text file contains `209870`
for “Own on Steam” or `480` for “Don't own on Steam”.

Do not remove the installer's saved state directory if you want to resume an
interrupted installation. It stores a hash of the ZIP URL and the server's
validators, not the URL itself.

The installer code is split by responsibility: configuration, download/resume,
ZIP extraction, saved state, terminal progress, and user prompts/game launching
are in separate `installer_*.py` modules. `installer.py` remains the entry point
and installation coordinator.

## Build a single executable

The single-file executable bundles Python and the configured `config.json`.
PyInstaller builds a native executable for the operating system it runs on, so
build separately on Windows and Linux. Python is required only on the build
machine; end users do not need to install Python.

1. Install the build dependency: `python -m pip install -r requirements-build.txt`
   (use `python3` on Linux). The build scripts use `.venv` automatically when
   present.
2. Run `.\build.ps1` on Windows or `sh build.sh` on Linux.
3. Find the executable in `dist/` (`BLRInstaller.exe` on Windows).

The ZIP URL and game launch arguments from `config.json` are bundled into the
executable. An optional `config.json` placed beside it overrides the bundled
configuration.
