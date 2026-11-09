"""Separate read-only application files from a user's writable library."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))


def application_root() -> Path:
    """The source checkout or the directory containing the distributed EXE."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    """PyInstaller's internal resource directory, distinct from the EXE folder."""
    if is_frozen() and getattr(sys, '_MEIPASS', None):
        return Path(sys._MEIPASS).resolve()
    return application_root()


def default_data_dir(demo: bool = False) -> Path:
    if not is_frozen():
        return application_root() / 'data' / ('demo' if demo else 'personal')
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    directory = base / 'Proxmark Studio'
    return directory / 'demo' if demo else directory


def firmware_catalog_dir(project_root: Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else application_root()
    return root / 'firmware' if is_frozen() else root / 'outputs' / 'device-setup'


def startup_error_path() -> Path:
    return default_data_dir() / 'startup-error.log'
