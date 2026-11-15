# -*- coding: utf-8 -*-
"""Синхронизация каталога с ModLand.

Сканирует страницы авторов, находит новые машины и карты, добирает
метаданные, читает конфиги из ZIP через HTTP Range (без полной загрузки),
скачивает обложки и галереи, проверяет записи и обновляет
tools/catalog_source.json.

Запуск:
  python tools/modland_sync.py --dry-run
  python tools/modland_sync.py --limit 5
  python tools/modland_sync.py --page <url мода> --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.remotezip import (  # noqa: E402
    RemoteZip,
    RemoteZipError,
    group_configs,
    probe_size,
    read_vehicle_configs,
)
from backend.sources import modland as modland_source  # noqa: E402
from backend.sources import modsfire as modsfire_source  # noqa: E402
from backend.sources.http import (  # noqa: E402
    SourceError,
    is_archive_url,
    is_modsfire,
    make_session,
)

MODLAND = "https://www.modland.net"
DEFAULT_CONFIG = ROOT / "tools" / "catalog_sources.json"
SOURCE = ROOT / "tools" / "catalog_source.json"
PENDING = ROOT / "tools" / "pending.json"
REPORT = ROOT / "tools" / "sync_report.json"
ASSETS = ROOT / "catalog_site" / "assets" / "mods"

MONTHS_RU = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]
AUTHOR_ALIASES = {"bubon": "bubon2012"}

DEFAULT_SOURCES = {
    "modland": {
        "authors": ["bubon", "ultimate-drag-complex", "superplayart"],
        "categories": ["cars", "maps"],
        "max_new_per_run": 20,
        "request_delay": 1.2,
        "auto_publish": True,
    },
    "catalog": {"gallery_images": 4},
}


def log(message: str) -> None:
    print(message, flush=True)


class Limiter:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, float(delay))
        self.last = 0.0

    def wait(self) -> None:
        delta = self.delay - (time.monotonic() - self.last)
        if delta > 0:
            time.sleep(delta)
        self.last = time.monotonic()


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def page_key(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    path = parts.path.rstrip("/").lower()
    if path.endswith("/index.html"):
        path = path[: -len("/index.html")]
    return path


def canonical(url: str) -> str:
    parts = urlsplit(urljoin(MODLAND, url))
    return f"https://www.modland.net{parts.path}" + (f"?{parts.query}" if parts.query else "")


def slugify(value: str, fallback: str = "mod") -> str:
    value = value.lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or fallback


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def format_downloads(value: int) -> str:
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
    elif value >= 1000:
        text = f"{value / 1000:.1f}K"
    else:
        text = str(value)
    return text.replace(".0K", "K").replace(".0M", "M")


def format_size(size_bytes: int) -> str:
    if not size_bytes:
        return ""
    mib = size_bytes / (1024 * 1024)
    if mib >= 1024:
        return f"{mib / 1024:.1f} ГБ".replace(".", ",")
    return f"{mib:.0f} МБ"


def parse_relative_date(text: str) -> str:
    raw = clean(text).lower()
    raw = re.sub(r"^(updated|added|обновлено|добавлено)\s+", "", raw)
    now = datetime.now(timezone.utc).date()
    if not raw:
        return ""
    if raw in ("today", "сегодня"):
        return f"{now.day} {MONTHS_RU[now.month - 1]} {now.year}"
    if raw in ("yesterday", "вчера"):
        day = now - timedelta(days=1)
        return f"{day.day} {MONTHS_RU[day.month - 1]} {day.year}"
    match = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        days = {
            "minute": 0,
            "hour": 0,
            "day": amount,
            "week": amount * 7,
            "month": amount * 30,
            "year": amount * 365,
        }[unit]
        day = now - timedelta(days=days)
        return f"{day.day} {MONTHS_RU[day.month - 1]} {day.year}"
    return clean(text)


def fetch_html(session: requests.Session, url: str, limiter: Limiter, tries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(tries):
        try:
            limiter.wait()
            response = session.get(url, timeout=(30, 60))
            if response.status_code >= 400:
                raise SourceError(f"HTTP {response.status_code}")
            return response.text
        except (requests.RequestException, SourceError) as exc:
            last_error = exc
            if attempt + 1 < tries:
                time.sleep(1.2 * (attempt + 1))
    raise SourceError(f"Не удалось открыть {url}: {last_error}")


def parse_author_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict] = []
    for card in soup.select("div.card.card-sm.mb-2"):
        anchor = card.select_one("h5.card-title a[href*='beamng.drive-mods/']")
        if anchor is None or not anchor.get("href"):
            continue
        title_text = clean(anchor.get_text(" ", strip=True))
        if not title_text:
            continue
        text = clean(card.get_text(" | ", strip=True))
        segments = [clean(part) for part in text.split("|") if clean(part)]
        version = ""
        if len(segments) > 1 and re.fullmatch(r"\d+(?:\.\d+)+[a-zA-Z]?", segments[1]):
            version = segments[1]
        size_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(MB|GB)", text, re.IGNORECASE)
        size_text = ""
        if size_match:
            size_text = f"{size_match.group(1)} {size_match.group(2).upper()}"
        downloads = 0
        downloads_match = re.search(r"(?:MB|GB)\s*\|\s*(\d+)", text, re.IGNORECASE)
        if downloads_match:
            downloads = int(downloads_match.group(1))
        else:
            numbers = re.findall(r"\|\s*(\d{2,})\s*(?=\|)", text)
            if numbers:
                downloads = int(numbers[-1])
        updated = ""
        for segment in segments:
            if re.match(r"^(updated|added)\s+", segment, re.IGNORECASE):
                updated = parse_relative_date(segment)
                break
        author = ""
        for index, segment in enumerate(segments[:-1]):
            if segment.lower() == "by":
                author = segments[index + 1]
                break
        category = ""
        for label in ("Cars", "Maps"):
            if re.search(rf"(^|\|)\s*{label}\s*(\||$)", text):
                category = label.lower()
                break
        game_version = ""
        match = re.search(r"\b(0\.\d{2}\.x)\b", text)
        if match:
            game_version = match.group(1)
        image = ""
        img = card.select_one("img")
        if img is not None:
            image = img.get("src") or img.get("data-src") or ""
            image = urljoin(MODLAND, image)
        cards.append(
            {
                "url": canonical(anchor["href"]),
                "title": title_text,
                "version": version,
                "size": size_text,
                "downloads": downloads,
                "updated": updated,
                "author": author,
                "category": category,
                "gameVersion": game_version,
                "image": image,
            }
        )
    return cards


def fetch_author_cards(
    session: requests.Session, author_url: str, limiter: Limiter, max_pages: int = 20
) -> list[dict]:
    html = fetch_html(session, author_url, limiter)
    cards = parse_author_cards(html)
    soup = BeautifulSoup(html, "html.parser")
    user_field = soup.select_one("#mods-filters input[name=user_id]")
    if user_field is None or not user_field.get("value"):
        return cards

    seen = {card["url"] for card in cards}
    page = 2
    while page <= max_pages:
        limiter.wait()
        try:
            response = session.get(
                f"{MODLAND}/api/mods",
                params={"user_id": user_field["value"], "layout": "cards", "page": page},
                headers={"Referer": author_url},
                timeout=(30, 60),
            )
            if response.status_code >= 400:
                break
            data = response.json()
        except (requests.RequestException, ValueError):
            break
        batch = parse_author_cards(str(data.get("mods") or ""))
        for card in batch:
            if card["url"] in seen:
                continue
            seen.add(card["url"])
            cards.append(card)
        total = data.get("total")
        next_page = data.get("next")
        if not batch or not next_page or (isinstance(total, int) and len(cards) >= total):
            break
        page = int(next_page) if str(next_page).isdigit() else page + 1
    return cards


def _description_text(soup: BeautifulSoup) -> list[str]:
    block = soup.select_one(".description-body") or soup.select_one(".post-description")
    if block is None:
        return []
    for br in block.find_all("br"):
        br.replace_with("\n")
    for item in block.find_all("li"):
        item.insert_before("\n- ")
        item.append("\n")
    raw = block.get_text("\n", strip=True)
    lines = [clean(line) for line in raw.splitlines()]
    return [line for line in lines if line]


def _is_bullet(line: str) -> bool:
    return bool(re.match(r"^[\-•*·]\s*", line))


def _bullet_text(line: str) -> str:
    return re.sub(r"^[\-•*·]\s*", "", line).strip()


def parse_mod_page(html: str, url: str, card_image: str = "") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))

    h1 = soup.select_one("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        title_tag = soup.select_one("title")
        title = clean(title_tag.get_text(" ", strip=True)) if title_tag else ""
        title = re.sub(r"\s+-\s+BeamNG\.drive.*$", "", title)

    download_anchor = soup.select_one("a[href*='/download/']")
    download_url = ""
    filename = ""
    block_text = ""
    if download_anchor is not None and download_anchor.get("href"):
        download_url = canonical(download_anchor["href"])
        anchor_text = clean(download_anchor.get_text(" ", strip=True))
        match = re.search(r"([\w.\-]+\.(?:zip|rar|7z))", anchor_text, re.IGNORECASE)
        if match:
            filename = match.group(1)
        node = download_anchor
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            text = clean(node.get_text(" ", strip=True))
            if "Version:" in text and "Total Size:" in text:
                block_text = text
                break

    version = ""
    updated_raw = ""
    size_text = ""
    if block_text:
        match = re.search(r"Version:\s*([\w.\-]+)", block_text)
        if match:
            version = match.group(1)
        match = re.search(r"Updated:\s*(.+?)\s*Total Size:", block_text)
        if match:
            updated_raw = match.group(1)
        match = re.search(r"Total Size:\s*([\d.,]+)\s*(MB|GB)", block_text, re.IGNORECASE)
        if match:
            size_text = f"{match.group(1)} {match.group(2).upper()}"
    if not filename:
        match = re.search(r"([\w.\-]+\.(?:zip|rar|7z))", page_text, re.IGNORECASE)
        if match:
            filename = match.group(1)

    stat_nodes = soup.select(".stats-item")
    stats = [clean(item.get_text(" ", strip=True)) for item in stat_nodes]
    stats_text = " | ".join(stats)
    downloads = 0
    match = re.search(r"([\d,]+)\s+downloads?", stats_text, re.IGNORECASE)
    if match:
        downloads = int(match.group(1).replace(",", ""))
    if not size_text:
        match = re.search(r"([\d.,]+)\s*(MB|GB)\s+files size", stats_text, re.IGNORECASE)
        if match:
            size_text = f"{match.group(1)} {match.group(2).upper()}"
    updated = parse_relative_date(updated_raw)
    if not updated:
        for item in stats:
            match = re.search(r"\b(?:by\s+.+?)\s+((?:\d+\s+\w+\s+ago)|today|yesterday)$", item, re.IGNORECASE)
            if match:
                updated = parse_relative_date(match.group(1))
                break
    author = ""
    author_page = ""
    for node in stat_nodes:
        item = clean(node.get_text(" ", strip=True))
        if not re.search(r"\bby\b", item):
            continue
        link = node.select_one("a[href*='/user/']")
        if link is not None:
            author = clean(link.get_text(" ", strip=True))
            author_page = canonical(link["href"])
            break
        match = re.search(r"\bby\s+(.+?)\s+\d+\s+\w+\s+ago", item, re.IGNORECASE)
        if match:
            author = clean(match.group(1))
            break
    if not author:
        link = soup.select_one("a[href*='/user/'][href]:not([href*='#'])")
        if link is not None:
            author = clean(link.get_text(" ", strip=True))
            author_page = canonical(link["href"])
    author = AUTHOR_ALIASES.get(author.lower(), author)

    category = ""
    breadcrumbs = [clean(a.get_text(" ", strip=True)) for a in soup.select(".breadcrumb a")]
    if "Maps" in breadcrumbs:
        category = "map"
    elif "Cars" in breadcrumbs:
        category = "car"
    elif "/maps/" in urlsplit(url).path:
        category = "map"
    elif "/cars/" in urlsplit(url).path:
        category = "car"

    game_version = ""
    match = re.search(r"\b(0\.\d{2}\.x)\b", page_text)
    if match:
        game_version = match.group(1)

    rating = 4.0
    ratings_text = ""
    match = re.search(r"([\d.]+)\s*/\s*5,\s*(\d+)\s+votes?", page_text, re.IGNORECASE)
    if match:
        rating = round(float(match.group(1)), 1)
        votes = int(match.group(2))
        ratings_text = f"{votes} {plural_ru(votes, 'оценка', 'оценки', 'оценок')}"
    else:
        likes_match = re.search(r"(\d+)\s+likes?", page_text, re.IGNORECASE)
        dislikes_match = re.search(r"(\d+)\s+dislikes?", page_text, re.IGNORECASE)
        likes = int(likes_match.group(1)) if likes_match else 0
        dislikes = int(dislikes_match.group(1)) if dislikes_match else 0
        if likes + dislikes:
            rating = round(5 * likes / (likes + dislikes), 1)
            ratings_text = f"{likes + dislikes} {plural_ru(likes + dislikes, 'оценка', 'оценки', 'оценок')}"

    if not version:
        match = re.search(r"\s(\d+(?:\.\d+)+[a-zA-Z]?)$", title)
        if match:
            version = match.group(1)
    if version and title.endswith(" " + version):
        title = title[: -(len(version) + 1)].strip()

    lines = _description_text(soup)
    headings = {"description", "features", "описание", "особенности"}
    features = []
    about_parts = []
    after_features = False
    for line in lines:
        lowered = line.lower().strip(" :")
        if lowered in headings:
            after_features = lowered in ("features", "особенности")
            continue
        if _is_bullet(line):
            text = _bullet_text(line)
            if text:
                features.append(text)
            after_features = True
            continue
        if after_features and len(features) < 8 and len(line) < 160:
            features.append(line)
            continue
        about_parts.append(line)
    about = clean(" ".join(about_parts))
    if len(about) > 900:
        about = about[:900].rsplit(" ", 1)[0] + "…"

    urls: list[str] = []
    gallery_block = soup.select_one("div.images-gallery.mx-3")
    if gallery_block is not None:
        anchors = [
            urljoin(MODLAND, a["href"]) for a in gallery_block.select("a.images-gallery-item[href]")
        ]
    else:
        anchors = [
            urljoin(MODLAND, a["href"]) for a in soup.select("a.images-gallery-item[href]")
        ]
        hash_dir = ""
        match = re.search(r"/i/([^/]+)/", card_image or "")
        if match:
            hash_dir = match.group(1)
        if not hash_dir:
            for anchor in anchors:
                match = re.search(r"/i/([^/]+)/", anchor)
                if match:
                    hash_dir = match.group(1)
                    break
        if hash_dir:
            anchors = [anchor for anchor in anchors if f"/i/{hash_dir}/" in anchor]
    seen_bases: set[str] = set()
    for anchor in anchors:
        base = re.sub(r"-(?:lg|md|sm)_modland\.(?:webp|jpg|jpeg|png)$", "", anchor, flags=re.IGNORECASE)
        if base in seen_bases:
            continue
        seen_bases.add(base)
        urls.append(anchor)
    if not urls:
        background = soup.select_one("img.background")
        if background is not None and background.get("src"):
            urls.append(urljoin(MODLAND, background["src"]))
    if not urls and card_image:
        urls.append(card_image)
    cover_url = urls[0] if urls else (card_image or "")

    return {
        "title": title,
        "type": category,
        "version": version,
        "sizeText": size_text,
        "downloads": downloads,
        "rating": rating,
        "ratings": ratings_text,
        "author": author,
        "authorPage": author_page,
        "updated": updated,
        "gameVersion": game_version,
        "about": about,
        "features": features,
        "galleryUrls": urls[:6],
        "coverUrl": cover_url,
        "page": canonical(url),
        "download": {
            "kind": "modland",
            "url": download_url,
            "file": filename,
        },
        "descriptionCount": 0,
    }


def resolve_direct(session: requests.Session, download: dict, limiter: Limiter) -> str:
    url = str(download.get("url") or "")
    kind = str(download.get("kind") or "").lower()
    if not url:
        raise SourceError("нет ссылки на скачивание")
    if kind == "modsfire" or is_modsfire(url):
        return modsfire_source.resolve(session, url)
    if kind == "modland":
        next_url = modland_source.resolve(session, url)
        if is_modsfire(next_url):
            return modsfire_source.resolve(session, next_url)
        if is_archive_url(next_url):
            return next_url
        limiter.wait()
        response = session.get(
            next_url,
            headers={"Accept": "application/octet-stream,*/*"},
            timeout=(30, 60),
            allow_redirects=True,
            stream=True,
        )
        try:
            if response.status_code >= 400:
                raise SourceError(f"сервер скачивания ответил {response.status_code}")
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise SourceError("источник вернул страницу вместо файла")
            return response.url
        finally:
            response.close()
    return url


def image_bytes(session: requests.Session, url: str, limiter: Limiter) -> bytes:
    limiter.wait()
    response = session.get(
        url,
        headers={"Referer": MODLAND + "/", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        timeout=(30, 60),
    )
    if response.status_code >= 400:
        raise SourceError(f"картинка: HTTP {response.status_code}")
    if len(response.content) > 20 * 1024 * 1024:
        raise SourceError("картинка слишком большая")
    return response.content


def save_webp(data: bytes, path: Path, size: tuple[int, int]) -> None:
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (13, 13, 14))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "WEBP", quality=82, method=6)


def save_images(
    session: requests.Session,
    urls: list[str],
    mod_id: str,
    limiter: Limiter,
    gallery_limit: int = 4,
) -> tuple[str, list[str], str, list[str]]:
    errors: list[str] = []
    saved: list[Path] = []
    for url in urls[: max(1, gallery_limit)]:
        try:
            data = image_bytes(session, url, limiter)
            number = len(saved) + 1
            path = ASSETS / f"{mod_id}-{number}.webp"
            save_webp(data, path, (1080, 1080))
            saved.append(path)
            if number == 1:
                save_webp(data, ASSETS / f"{mod_id}-cover.webp", (1080, 1080))
                save_webp(data, ASSETS / f"{mod_id}-thumb.webp", (640, 640))
        except Exception as exc:  # noqa: BLE001 - reported per image
            errors.append(f"{url}: {exc}")
            continue
    if not saved:
        return "", [], "", errors
    cover = f"assets/mods/{mod_id}-cover.webp"
    thumb = f"assets/mods/{mod_id}-thumb.webp"
    gallery = [f"assets/mods/{mod_id}-{index}.webp" for index in range(1, len(saved) + 1)]
    return cover, gallery, thumb, errors


def refresh_images(
    session: requests.Session,
    mods: list[dict],
    gallery_limit: int,
    limiter: Limiter,
) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    problems: list[str] = []
    for mod in mods:
        page = str(mod.get("page") or "")
        mod_id = str(mod.get("id") or "")
        if not page or not mod_id:
            continue
        try:
            html = fetch_html(session, page, limiter)
            parsed = parse_mod_page(html, page)
            urls = list(parsed.get("galleryUrls") or [])
            if not urls:
                problems.append(f"{mod_id}: на странице нет галереи")
                continue
            old_names = {
                Path(str(mod.get(key) or "")).name
                for key in ("img", "cover")
            } | {Path(str(item)).name for item in (mod.get("gallery") or [])}
            cover, gallery, thumb, errors = save_images(
                session, urls, mod_id, limiter, gallery_limit
            )
            if not cover:
                problems.append(f"{mod_id}: не удалось скачать картинки: {'; '.join(errors)}")
                continue
            mod["cover"] = cover
            mod["img"] = thumb
            mod["gallery"] = gallery
            new_names = {Path(cover).name, Path(thumb).name} | {
                Path(item).name for item in gallery
            }
            for name in old_names - new_names:
                stale = ASSETS / name
                if stale.is_file():
                    stale.unlink()
            changed.append(mod_id)
            log(f"  обновлены картинки: {mod_id} ({len(gallery)} шт.)")
        except Exception as exc:  # noqa: BLE001 - reported per mod
            problems.append(f"{mod_id}: {exc}")
    return changed, problems


def generated_text(mod: dict, config_count: int) -> tuple[str, str, list[str]]:
    title = mod["title"]
    game_version = mod.get("gameVersion") or ""
    about = mod.get("about") or ""
    short = ""
    if about:
        first = re.split(r"(?<=[.!?])\s+", about, maxsplit=1)[0].strip()
        generic = first.lower().startswith("mod ") and "beamng" in first.lower()
        if first and len(first) >= 30 and not generic:
            short = first
    if not short:
        if mod["type"] == "car":
            short = f"{title} для BeamNG.drive."
            if config_count:
                short = f"{title} для BeamNG.drive — {config_count} конфигураций."
        else:
            short = f"{title} — карта для BeamNG.drive."
    if len(short) > 160:
        short = short[:157].rsplit(" ", 1)[0] + "…"

    features = [clean(item) for item in (mod.get("features") or []) if clean(item)]
    features = list(dict.fromkeys(features))[:8]
    if not features:
        if mod["type"] == "car":
            if config_count:
                features.append(f"{config_count} конфигураций")
            if game_version:
                features.append(f"Поддержка BeamNG.drive {game_version}")
        else:
            if game_version:
                features.append(f"Карта для BeamNG.drive {game_version}")
            else:
                features.append("Карта для BeamNG.drive")
        features.append("Скачано с ModLand")

    if not about:
        if mod["type"] == "car":
            about = f"{title} для BeamNG.drive."
            if config_count:
                about += f" В моде {config_count} конфигураций."
        else:
            about = f"{title} — карта для BeamNG.drive."
    return short, about, features


def unique_id(base: str, used: set[str]) -> str:
    candidate = slugify(base)
    if candidate not in used:
        used.add(candidate)
        return candidate
    for index in range(2, 1000):
        candidate = f"{slugify(base)}-{index}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError("не удалось подобрать id")


def build_entry(
    session: requests.Session,
    card: dict,
    config: dict,
    limiter: Limiter,
    used_ids: set[str],
    dry_run: bool,
) -> tuple[dict | None, list[str]]:
    diagnostics: list[str] = []
    html = fetch_html(session, card["url"], limiter)
    mod = parse_mod_page(html, card["url"], card.get("image") or "")
    if mod["type"] not in ("car", "map"):
        diagnostics.append(f"категория не поддерживается: {mod['type'] or '?'}")
        return None, diagnostics
    if not mod["download"]["url"]:
        diagnostics.append("на странице нет ссылки /download/")
        return None, diagnostics

    direct = resolve_direct(session, mod["download"], limiter)
    size_bytes = 0
    configs: list[dict] = []
    groups: list[dict] = []
    if mod["type"] == "car":
        remote = RemoteZip(direct, session=session)
        try:
            with remote as archive:
                configs = read_vehicle_configs(archive)
            size_bytes = remote.size
        finally:
            try:
                remote.zip.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
            try:
                remote.buffer.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
            try:
                remote.remote.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
        groups = group_configs(configs)
        if not configs:
            diagnostics.append("в архиве не найдены info_config_*.json")
    else:
        size_bytes = probe_size(direct, session=session)

    if not size_bytes:
        diagnostics.append("не удалось определить размер архива")

    base = urlsplit(mod["page"]).path.rsplit("/", 1)[-1]
    base = re.sub(r"\.html$", "", base, flags=re.IGNORECASE)
    mod_id = unique_id(base or mod["title"], used_ids)

    cover = gallery = thumb = ""
    image_errors: list[str] = []
    if not dry_run:
        cover, gallery, thumb, image_errors = save_images(
            session,
            mod["galleryUrls"],
            mod_id,
            limiter,
            int(config.get("gallery_images") or 4),
        )
        if not cover:
            diagnostics.append("не удалось скачать ни одной картинки")
    else:
        cover = mod.get("coverUrl") or ""
        gallery = list(mod.get("galleryUrls") or [])
        thumb = cover

    short, about, features = generated_text(mod, len(configs))
    downloads = int(mod.get("downloads") or card.get("downloads") or 0)
    entry = {
        "id": mod_id,
        "type": mod["type"],
        "title": mod["title"],
        "short": short,
        "about": about,
        "img": thumb,
        "cover": cover,
        "gallery": gallery,
        "downloads": format_downloads(downloads),
        "downloadsFull": str(downloads),
        "rating": mod.get("rating") or 4.0,
        "ratings": mod.get("ratings") or "",
        "author": mod.get("author") or card.get("author") or "",
        "version": mod.get("version") or card.get("version") or "1.0",
        "size": format_size(size_bytes),
        "sizeBytes": size_bytes,
        "source": "ModLand",
        "updated": mod.get("updated") or card.get("updated") or "",
        "gameVersion": mod.get("gameVersion") or card.get("gameVersion") or "",
        "configs": [item["name"] for item in configs],
        "configGroups": groups,
        "features": features,
        "page": mod["page"],
        "authorPage": mod.get("authorPage") or "",
        "download": mod["download"],
    }
    if not entry["updated"]:
        now = datetime.now(timezone.utc).date()
        entry["updated"] = f"{now.day} {MONTHS_RU[now.month - 1]} {now.year}"
    if entry["author"] and entry["author"].lower() in AUTHOR_ALIASES:
        entry["author"] = AUTHOR_ALIASES[entry["author"].lower()]

    if entry["type"] == "car" and not entry["configs"]:
        diagnostics.append("у машины нет конфигураций")
    if entry["sizeBytes"] < 100_000:
        diagnostics.append("архив подозрительно маленький")
    if not dry_run and not entry["cover"]:
        diagnostics.append("нет обложки")
    diagnostics.extend(image_errors)
    return (entry if not diagnostics else None), diagnostics


def update_from_card(entry: dict, card: dict) -> bool:
    changed = False
    for key in ("version", "updated", "gameVersion", "author"):
        value = card.get(key)
        if value and entry.get(key) != value:
            entry[key] = value
            changed = True
    if card.get("size"):
        match = re.match(r"([\d.,]+)\s*(MB|GB)", card["size"], re.IGNORECASE)
        if match:
            number = float(match.group(1).replace(",", "."))
            factor = 1024 ** 3 if match.group(2).upper() == "GB" else 1024 ** 2
            size = format_size(int(number * factor))
            if entry.get("size") != size:
                entry["size"] = size
                changed = True
    downloads = int(card.get("downloads") or 0)
    if downloads and str(downloads) != str(entry.get("downloadsFull") or ""):
        entry["downloadsFull"] = str(downloads)
        entry["downloads"] = format_downloads(downloads)
        changed = True
    return changed


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def load_json(path: Path, default: object) -> object:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Синхронизация каталога с ModLand")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    parser.add_argument("--limit", type=int, default=0, help="максимум новых модов за запуск")
    parser.add_argument("--page", default="", help="проверить одну страницу мода")
    parser.add_argument("--no-build", action="store_true", help="не пересобирать catalog.json")
    parser.add_argument("--refresh-images", action="store_true", help="перекачать картинки всех модов")
    parser.add_argument("--id", default="", help="обновить только указанные id (через запятую)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    raw_config = load_json(config_path, DEFAULT_SOURCES)
    config = DEFAULT_SOURCES | (raw_config if isinstance(raw_config, dict) else {})
    modland_config = DEFAULT_SOURCES["modland"] | (config.get("modland") or {})
    catalog_config = DEFAULT_SOURCES["catalog"] | (config.get("catalog") or {})
    categories = {str(item).lower() for item in (modland_config.get("categories") or [])}
    delay = float(modland_config.get("request_delay") or 1.2)
    limit = args.limit or int(modland_config.get("max_new_per_run") or 20)
    auto_publish = bool(modland_config.get("auto_publish", True))
    started = datetime.now(timezone.utc)
    limiter = Limiter(delay)
    session = make_session()

    source_data = load_json(SOURCE, {"appVersion": "1.0.0", "sources": [], "mods": []})
    if not isinstance(source_data, dict):
        source_data = {"appVersion": "1.0.0", "sources": [], "mods": []}
    mods = list(source_data.get("mods") or [])
    used_ids = {str(item.get("id")) for item in mods if item.get("id")}
    known = {page_key(str(item.get("page") or "")): item for item in mods if item.get("page")}
    pending_data = load_json(PENDING, [])
    pending = list(pending_data) if isinstance(pending_data, list) else []
    pending_pages = {page_key(str(item.get("page") or "")) for item in pending}

    report = {
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": "",
        "authors": [],
        "added": [],
        "updated": [],
        "pending": [],
        "errors": [],
        "dryRun": args.dry_run,
    }

    if args.refresh_images:
        selected = mods
        wanted = {item.strip() for item in args.id.split(",") if item.strip()}
        if wanted:
            selected = [mod for mod in mods if str(mod.get("id")) in wanted]
        changed, problems = refresh_images(
            session, selected, int(catalog_config.get("gallery_images") or 4), limiter
        )
        report["imagesChanged"] = changed
        report["errors"].extend(problems)
        report["finishedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not args.dry_run and changed:
            source_data["mods"] = mods
            source_data.setdefault("appVersion", "1.0.0")
            source_data.setdefault("sources", [])
            write_json(SOURCE, source_data)
            if not args.no_build:
                subprocess.run([sys.executable, str(ROOT / "tools" / "build_catalog.py")], check=True)
        write_json(REPORT, report)
        log(f"итог: обновлено картинок {len(changed)}, проблем {len(problems)}")
        return 0

    if args.page:
        card = {"url": canonical(args.page), "image": ""}
        try:
            entry, diagnostics = build_entry(
                session, card, catalog_config, limiter, set(used_ids), True
            )
        except Exception as exc:  # noqa: BLE001 - debug output
            log(f"ОШИБКА: {exc}")
            return 1
        if entry:
            entry["imagesPreview"] = entry.get("gallery")
        print(json.dumps({"entry": entry, "diagnostics": diagnostics}, ensure_ascii=False, indent=2))
        return 0 if entry else 2

    authors = [str(item).strip().lstrip("@") for item in (modland_config.get("authors") or []) if str(item).strip()]
    if not authors:
        report["errors"].append("в конфиге нет авторов ModLand")
    new_count = 0
    for author in authors:
        author_url = f"{MODLAND}/user/{author}"
        report["authors"].append(author_url)
        try:
            cards = fetch_author_cards(session, author_url, limiter)
        except Exception as exc:  # noqa: BLE001 - reported per author
            report["errors"].append(f"{author_url}: {exc}")
            log(f"[{author}] ошибка: {exc}")
            continue
        log(f"[{author}] найдено модов: {len(cards)}")
        for card in cards:
            if categories and card.get("category") and card["category"] not in categories:
                continue
            key = page_key(card["url"])
            existing = known.get(key)
            if existing is not None:
                if update_from_card(existing, card):
                    if existing["id"] not in [item["id"] for item in report["updated"]]:
                        report["updated"].append(
                            {"id": existing["id"], "title": existing["title"], "version": existing.get("version", "")}
                        )
                    if args.verbose:
                        log(f"[{author}] обновлён {existing['title']} {existing.get('version', '')}")
                continue
            if key in pending_pages:
                continue
            if new_count >= limit:
                continue
            if not auto_publish:
                pending.append(
                    {
                        "page": card["url"],
                        "title": card["title"],
                        "reason": "auto_publish выключен",
                        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                )
                pending_pages.add(key)
                report["pending"].append({"title": card["title"], "page": card["url"], "reason": "auto_publish выключен"})
                continue
            log(f"[{author}] новый мод: {card['title']} — {card['url']}")
            try:
                entry, diagnostics = build_entry(
                    session, card, catalog_config, limiter, used_ids, args.dry_run
                )
            except Exception as exc:  # noqa: BLE001 - reported per mod
                entry = None
                diagnostics = [str(exc)]
            if entry is None:
                reason = "; ".join(dict.fromkeys(diagnostics)) or "не прошёл проверку"
                pending.append(
                    {
                        "page": card["url"],
                        "title": card["title"],
                        "reason": reason,
                        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                )
                pending_pages.add(key)
                report["pending"].append({"title": card["title"], "page": card["url"], "reason": reason})
                log(f"[{author}]   пропущен: {reason}")
                continue
            mods.append(entry)
            used_ids.add(entry["id"])
            known[key] = entry
            new_count += 1
            report["added"].append(
                {
                    "id": entry["id"],
                    "title": entry["title"],
                    "type": entry["type"],
                    "version": entry["version"],
                    "size": entry["size"],
                    "configs": len(entry["configs"]),
                }
            )
            log(
                f"[{author}]   добавлен {entry['id']}: {entry['version']}, {entry['size']}, "
                f"{len(entry['configs'])} конфигов"
            )

    report["finishedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not args.dry_run:
        source_data["mods"] = mods
        source_data.setdefault("appVersion", "1.0.0")
        source_data.setdefault("sources", [])
        write_json(SOURCE, source_data)
        write_json(PENDING, pending)
        if not args.no_build and (report["added"] or report["updated"]):
            subprocess.run([sys.executable, str(ROOT / "tools" / "build_catalog.py")], check=True)
        write_json(REPORT, report)

    log(
        "итог: добавлено {added}, обновлено {updated}, в ожидании {pending}, ошибок {errors}".format(
            added=len(report["added"]),
            updated=len(report["updated"]),
            pending=len(report["pending"]),
            errors=len(report["errors"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
