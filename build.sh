#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
    .venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/blr_installer.spec
else
    python3 -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/blr_installer.spec
fi
