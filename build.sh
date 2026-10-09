#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
    .venv/bin/python -m PyInstaller --noconfirm --clean BLRInstaller.spec
else
    python3 -m PyInstaller --noconfirm --clean BLRInstaller.spec
fi
