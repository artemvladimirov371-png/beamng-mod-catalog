from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "BeamNG Mod Installer"
APP_VERSION = "1.0.0"


def local_app_data() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base)
    return Path.home() / "AppData" / "Local"


def app_data_dir() -> Path:
    path = local_app_data() / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def default_downloads_dir() -> str:
    downloads = Path.home() / "Downloads" / "BeamNGMods"
    return str(downloads)


DEFAULT_SETTINGS = {
    "gamePath": "",
    "downloadPath": default_downloads_dir(),
    "gameVersion": "",
    "notifications": True,
    "gameFound": False,
}


class Store:
    def __init__(self) -> None:
        self.dir = app_data_dir()
        self.settings_path = self.dir / "settings.json"
        self.installed_path = self.dir / "installed.json"
        self._settings = {**DEFAULT_SETTINGS, **load_json(self.settings_path, {})}
        installed = load_json(self.installed_path, [])
        self._installed: list[dict] = installed if isinstance(installed, list) else []

    # ---------- settings ----------

    def settings(self) -> dict:
        return dict(self._settings)

    def update_settings(self, patch: dict) -> dict:
        for key in DEFAULT_SETTINGS:
            if key in patch:
                self._settings[key] = patch[key]
        save_json(self.settings_path, self._settings)
        return self.settings()

    # ---------- installed ----------

    def installed(self) -> list[dict]:
        return [dict(item) for item in self._installed]

    def get_installed(self, mod_id: str) -> dict | None:
        for item in self._installed:
            if item.get("id") == mod_id:
                return dict(item)
        return None

    def add_installed(self, record: dict) -> None:
        self._installed = [item for item in self._installed if item.get("id") != record.get("id")]
        self._installed.append(record)
        save_json(self.installed_path, self._installed)

    def remove_installed(self, mod_id: str) -> dict | None:
        found = None
        keep = []
        for item in self._installed:
            if item.get("id") == mod_id:
                found = item
            else:
                keep.append(item)
        if found is not None:
            self._installed = keep
            save_json(self.installed_path, self._installed)
        return dict(found) if found else None
