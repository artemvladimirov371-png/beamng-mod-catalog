from __future__ import annotations

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .http import READ_TIMEOUT, SourceError, is_archive_url, is_modsfire, meta_refresh

TIMEOUT = (30, READ_TIMEOUT)


class Cancelled(Exception):
    """The user cancelled the operation."""


def _wait(seconds: float, cancel=None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        time.sleep(0.25)
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def resolve(session: requests.Session, download_url: str, cancel=None) -> str:
    """Post the ModLand download form and return the next URL (ModsFire or file)."""
    response = session.get(download_url, timeout=TIMEOUT)
    if response.status_code >= 400:
        raise SourceError(f"ModLand: страница скачивания недоступна (HTTP {response.status_code})")

    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.select_one("form.download-form")
    if form is None:
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if is_modsfire(href):
                return href
            if is_archive_url(href):
                return href
        raise SourceError("ModLand не показал ссылку на скачивание")

    action = urljoin(response.url, form.get("action") or response.url)
    payload = {
        field.get("name"): field.get("value", "")
        for field in form.find_all("input")
        if field.get("name")
    }
    if not payload:
        raise SourceError("ModLand: не удалось подготовить запрос скачивания")

    wait_seconds = 5.0
    match = re.search(r"waitingTime\s*=\s*(\d+)", response.text)
    if match:
        wait_seconds = min(float(match.group(1)), 30.0)
    _wait(wait_seconds + 0.6, cancel)

    posted = session.post(
        action,
        data=payload,
        headers={"Referer": response.url},
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    location = posted.headers.get("Location", "")
    if not location and posted.status_code == 200:
        location = meta_refresh(posted.text, posted.url)
    if not location:
        if posted.status_code >= 400:
            raise SourceError(f"ModLand: сервер отклонил запрос (HTTP {posted.status_code})")
        raise SourceError("ModLand не вернул ссылку на файл")
    return urljoin(posted.url, location)
