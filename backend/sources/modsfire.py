from __future__ import annotations

import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .http import READ_TIMEOUT, SourceError, is_archive_url, meta_refresh

BASE = "https://modsfire.com"
TIMEOUT = (30, READ_TIMEOUT)


def _get(session: requests.Session, url: str, referer: str = "", allow_redirects: bool = True):
    headers = {"Referer": referer} if referer else {}
    return session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=allow_redirects)


def _download_link(soup: BeautifulSoup) -> str:
    link = soup.select_one("#downloadnow")
    if link and link.get("href"):
        return urljoin(BASE, link["href"])
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if "/d/" in href and "modsfire.com" in href:
            return urljoin(BASE, href)
    return ""


def resolve(session: requests.Session, page_url: str) -> str:
    """Resolve a ModsFire page into a direct, signed file URL."""
    response = _get(session, page_url)
    if response.status_code >= 400:
        raise SourceError(f"ModsFire: страница недоступна (HTTP {response.status_code})")

    soup = BeautifulSoup(response.text, "html.parser")
    step_url = page_url

    if not soup.select_one("#downloadnow"):
        generate = soup.select_one("a[href^='/download/']")
        if generate and generate.get("href"):
            step_url = urljoin(BASE, generate["href"])
            response = _get(session, step_url, referer=page_url)
            if response.status_code >= 400:
                raise SourceError("ModsFire не выдал ссылку на скачивание")
            soup = BeautifulSoup(response.text, "html.parser")

    link = _download_link(soup)
    if not link:
        raise SourceError("ModsFire не показал кнопку скачивания")

    location = ""
    for attempt in range(3):
        redirect = _get(session, link, referer=step_url, allow_redirects=False)
        if redirect.status_code in (301, 302, 303, 307, 308):
            location = redirect.headers.get("Location", "")
            break
        if redirect.status_code == 200:
            if is_archive_url(redirect.url):
                return redirect.url
            ctype = redirect.headers.get("Content-Type", "").lower()
            if ctype and "text/html" not in ctype:
                return redirect.url
            location = meta_refresh(redirect.text, redirect.url)
            if location:
                break
        if redirect.status_code in (403, 429):
            time.sleep(1.2 * (attempt + 1))
            continue
        raise SourceError(f"ModsFire: ошибка получения файла (HTTP {redirect.status_code})")

    if not location:
        raise SourceError("ModsFire не вернул адрес файла")

    # The intermediate host redirects to signed cloud storage. Follow it without
    # downloading the body: the next response only carries the final Location.
    intermediate = _get(session, location, referer=link, allow_redirects=False)
    if intermediate.status_code in (301, 302, 303, 307, 308):
        final = intermediate.headers.get("Location", "")
        if final:
            return final
    if intermediate.status_code == 200:
        ctype = intermediate.headers.get("Content-Type", "").lower()
        if is_archive_url(intermediate.url) or (ctype and "text/html" not in ctype):
            return intermediate.url
        follow = meta_refresh(intermediate.text, intermediate.url)
        if follow:
            final = _get(session, follow, referer=location, allow_redirects=False)
            if final.headers.get("Location"):
                return final.headers["Location"]
            if final.status_code == 200:
                return final.url
    raise SourceError(f"ModsFire: не удалось получить файл (HTTP {intermediate.status_code})")
