# -*- coding: utf-8 -*-
"""Собирает каталог приложения из tools/catalog_source.json.

Запуск:  python tools/build_catalog.py
Результат:
  prototype/data/catalog.json  — каталог для backend (локальные картинки)
  prototype/js/data.js         — тот же каталог для статического прототипа
  catalog_site/catalog.json    — каталог для GitHub Pages
  catalog_site/assets/mods/*   — картинки для GitHub Pages

Источник правды — tools/catalog_source.json. Его дополняет
tools/modland_sync.py; вручную править можно там же.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tools" / "catalog_source.json"
SITE = ROOT / "catalog_site"
LOCAL_ASSETS = ROOT / "prototype" / "assets" / "mods"
SITE_ASSETS = SITE / "assets" / "mods"

DEFAULT_SOURCES = [
    "https://www.modland.net/user/bubon",
    "https://stmods.org/beamng_drive/",
    "https://modsgaming.ru/load/beamng_drive/35",
    "https://www.beamng.com/resources/",
]


def load_source() -> dict:
    if not SOURCE.is_file():
        raise SystemExit(f"Не найден источник каталога: {SOURCE}")
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    mods = data.get("mods") if isinstance(data, dict) else data
    if not isinstance(mods, list) or not mods:
        raise SystemExit("В catalog_source.json нет списка mods")
    for mod in mods:
        if not isinstance(mod, dict) or not mod.get("id") or not mod.get("title"):
            raise SystemExit(f"Некорректная запись каталога: {mod!r}")
    return {
        "appVersion": str(data.get("appVersion") or "1.0.0"),
        "sources": list(data.get("sources") or DEFAULT_SOURCES),
        "mods": mods,
    }


def referenced_images(mods: list[dict]) -> set[str]:
    names: set[str] = set()

    def add(value: object) -> None:
        if not isinstance(value, str) or not value:
            return
        if value.startswith(("http://", "https://")):
            return
        name = Path(value).name
        if name:
            names.add(name)

    for mod in mods:
        add(mod.get("img"))
        add(mod.get("cover"))
        for item in mod.get("gallery") or []:
            add(item)
    return names


def copy_images(mods: list[dict]) -> list[str]:
    missing: list[str] = []
    names = referenced_images(mods)
    SITE_ASSETS.mkdir(parents=True, exist_ok=True)
    LOCAL_ASSETS.mkdir(parents=True, exist_ok=True)
    for name in sorted(names):
        source = SITE_ASSETS / name
        if not source.is_file():
            legacy = LOCAL_ASSETS / name
            if legacy.is_file():
                shutil.copy2(legacy, source)
        if source.is_file():
            shutil.copy2(source, LOCAL_ASSETS / name)
        else:
            missing.append(name)
    for folder in (SITE_ASSETS, LOCAL_ASSETS):
        for stale in folder.glob("*.webp"):
            if stale.name not in names:
                stale.unlink()
    return missing


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main() -> None:
    data = load_source()
    now = datetime.now(timezone.utc)
    catalog = {
        "version": now.strftime("%Y%m%dT%H%M%SZ"),
        "generatedAt": now.strftime("%Y-%m-%d"),
        "appVersion": data["appVersion"],
        "sources": data["sources"],
        "mods": data["mods"],
    }
    payload = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"

    local_dir = ROOT / "prototype" / "data"
    local_dir.mkdir(parents=True, exist_ok=True)
    write_text(local_dir / "catalog.json", payload)

    header = (
        "/* Каталог модов — сгенерировано tools/build_catalog.py.\n"
        "   Источник: tools/catalog_source.json (ModLand и ручные записи). */\n"
    )
    data_js = (
        header
        + "\nconst MODS = "
        + json.dumps(data["mods"], ensure_ascii=False, indent=2)
        + ";\n\n"
        + "function getMod(id) {\n"
        + "  return MODS.find(m => m.id === id) || null;\n"
        + "}\n"
    )
    write_text(ROOT / "prototype" / "js" / "data.js", data_js)

    SITE.mkdir(parents=True, exist_ok=True)
    site_catalog = SITE / "catalog.json"
    write_text(site_catalog, payload)
    digest = hashlib.sha256(site_catalog.read_bytes()).hexdigest()
    write_text(SITE / "catalog.sha256", f"{digest}  catalog.json\n")
    missing = copy_images(data["mods"])

    print(f"catalog: {len(data['mods'])} mods, version {catalog['version']}")
    print(f"json: {local_dir / 'catalog.json'}")
    print(f"js:   {ROOT / 'prototype' / 'js' / 'data.js'}")
    print(f"site: {SITE}")
    if missing:
        print("warning: нет файлов картинок:", ", ".join(missing))


if __name__ == "__main__":
    main()
