# Simple cross-platform installer

![Screenshot of v1.0.0](/screenshot.png)

This interactive installer downloads a ZIP file, extracts it into the selected
folder, and writes the selected Steam app ID to
`blacklightretribution/Binaries/Win32/steam_appid.txt` inside that folder.

The installer displays progress for downloading and extracting. Progress and
installation settings are saved in the user's application state directory
(`%LOCALAPPDATA%\BLRevive\installer` on Windows or
`~/.local/state/BLRevive/installer` on Linux, unless `XDG_STATE_HOME` is set).
HTTPS downloads validate certificates against the bundled `certifi` CA bundle,
so public certificates do not depend on the Windows root store being up to
date. Private roots used by TLS inspection still need to be trusted separately.
The installer remembers the last selected install folder. The initial folder is
`%LOCALAPPDATA%\BLRevive` on Windows and `~/Games` on Linux. The ZIP contains a
`blacklightretribution` folder, so that folder appears inside the selected
location.
After an install finishes, it adds or updates the BLRevive non-Steam shortcut
in the Steam library and asks whether to launch Steam. Steam is not launched
automatically. On Linux it also selects Proton Experimental for the shortcut; 
Steam can stay open
during the download and extraction. If it is still running when shortcut setup
begins, the installer asks you to close it because Steam can overwrite changes
to its shortcut files. If Steam has multiple user directories, the installer
prompts you to choose which library receives the shortcut, using each account's
display name and username where available. The Launch game button starts that
Steam library shortcut and closes the installer.

If an install is interrupted, the next run shows the saved progress and lets you
continue, change the Steam choice or install folder, or cancel while keeping the
saved progress. Downloads resume when the server supports HTTP range requests;
otherwise, they restart from the beginning. During extraction completed files and the current file offset are saved,
so extraction can resume without re-extracting completed data. Changing the
install folder resets extraction progress for that folder while retaining the
downloaded ZIP for resuming that install. Incomplete downloads use a `.zip.part`
suffix; after a complete ZIP is received, it is renamed to `.zip`. Progress is
shown as a percentage when the total size is known, or as transferred bytes
otherwise.
The GUI remembers the “Keep downloaded ZIP for faster reinstalls” checkbox
setting. When it is unchecked and no installation is in progress, the installer
asks whether to delete an existing cached ZIP now or keep it for the next
reinstall. If unchecked during an interrupted installation, the ZIP remains
available for resuming and is removed after installation completes.

On Windows, before configuring the Steam shortcut, the installer checks for the
DirectX June 2010 and Microsoft Visual C++ 2010 and 2012 x86 runtime DLLs. It
prompts to install only missing prerequisites; declining continues installation
without them. Each setup requests administrator approval through Windows UAC.
The installer reports setup failures and whether Windows needs to restart, but
never restarts automatically. These redistributable installers are bundled into the Windows
executable, increasing its size by about 116 MB; they are not bundled into Linux
builds.

The GUI has a checkbox for keeping the downloaded ZIP and a checkbox for having
Steam recognize the game as Blacklight: Retribution (requires owning the game on
Steam). When unchecked, it uses App ID 480. Before installation it reports
available space and warns about an existing game folder. Configured size
estimates are used until a complete ZIP is available. Download and extraction
progress includes a transfer rate and estimated time remaining when those can be
calculated. The GUI also allows choosing the install folder and uses one action
button to install, pause, or continue an installation. Closing the GUI while an
operation is active requests a pause and saves progress.

Do not remove the installer's saved state directory if you want to resume an
interrupted installation. It stores a hash of the ZIP URL and the server's
validators, not the URL itself.

## Configure

Edit `config.json` and set `zip_url` to the HTTPS or HTTP URL of the ZIP file:

```json
{
  "zip_url": "https://ddd-cloud.com/blrzip",
  "zip_size_gib": 5.6,
  "uncompressed_size_gib": 6.6,
  "launch_args": [
    "-zcureurl=blrrevive.ddd-game.de",
    "-zcureport=80",
    "-presenceurl=blrrevive.ddd-game.de",
    "-presenceport=9004"
  ]
}
```

`launch_args` is an array of arguments passed to the Steam shortcut. Omit it to
use the defaults shown above, or set it to an empty array for no launch arguments.
`zip_size_gib` and `uncompressed_size_gib` are optional size estimates in
gibibytes (GiB, where 1 GiB is 1024³ bytes), used by the GUI's pre-install
space check when the ZIP is not yet available. Once a complete ZIP is present,
the installer reads the actual uncompressed size from its contents.

## Run

Python 3 and the project dependencies must be installed:

```powershell
python -m pip install -r requirements.txt
```

- **GUI:** `py installer.py` on Windows or `python3 installer.py` on Linux.
- **Terminal:** add `--cli` to either command.

The GUI uses ttkbootstrap's built-in `bootstrap-dark` theme.

The terminal version prompts for whether you own the game on Steam, whether to
launch Steam after installation, whether to keep the downloaded ZIP, and for an
install folder (press Enter to use the remembered location or the platform
default above). The generated text file
contains `209870` for “Own on Steam” or `480` for “Don't own on Steam”.

The application code lives in the `blr_installer/` package, grouped by
responsibility. `installer.py` remains a small root-level launcher. Automated
tests are in `tests/`, icons are in `assets/`, and the PyInstaller spec is in
`packaging/`.

## Run tests

Run the test suite from the repository root:

```powershell
python -m unittest discover
```

## Build a single executable

The single-file executable bundles Python and the configured `config.json`.
PyInstaller builds a native executable for the operating system it runs on, so
build separately on Windows and Linux. Python is required only on the build
machine; end users do not need to install Python.

1. Install the build dependency: `python -m pip install -r requirements-build.txt`
   (use `python3` on Linux). The build scripts use `.venv` automatically when
   present.
2. Run `.\build.ps1` on Windows or `sh build.sh` on Linux.
3. Find the executable in `dist/` (`BLReviveInstallerV1.exe` on Windows).

The ZIP URL and game launch arguments from `config.json` are bundled into the
executable. An optional `config.json` placed beside it overrides the bundled
configuration.
