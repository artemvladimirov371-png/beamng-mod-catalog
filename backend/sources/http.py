from __future__ import annotations

import re
from urllib.parse import urljoin

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"

CONNECT_TIMEOUT = 30
READ_TIMEOUT = 60


class SourceError(Exception):
    """A download source did not return a usable link."""


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": ACCEPT,
            "Accept-Language": ACCEPT_LANGUAGE,
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return session


def meta_refresh(html: str, base: str = "") -> str:
    if not html:
        return ""
    match = re.search(
        r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*url=([^"'>]+)""",
        html,
        re.IGNORECASE,
    )
    if match:
        return urljoin(base, match.group(1).strip())
    match = re.search(r"""window\.location(?:\.href)?\s*=\s*["']([^"']+)["']""", html)
    if match:
        return urljoin(base, match.group(1).strip())
    return ""


def is_archive_url(url: str) -> bool:
    path = url.split("?", 1)[0].lower()
    return path.endswith((".zip", ".rar", ".7z", ".beamng"))


def is_modsfire(url: str) -> bool:
    return "modsfire.com" in url.lower()


def ensure_ok(response: requests.Response, what: str) -> None:
    if response.status_code >= 400:
        raise SourceError(f"{what}: сервер ответил {response.status_code}")
