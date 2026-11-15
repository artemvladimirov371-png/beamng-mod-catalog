from __future__ import annotations

import io
import json
import re
import time
import zipfile
from typing import Any

import requests

from .sources.http import READ_TIMEOUT, UA, make_session

DEFAULT_CHUNK = 1 << 20


class RemoteZipError(Exception):
    """Could not read a remote archive."""


class RangeFile(io.RawIOBase):
    """Read-only file object backed by HTTP Range requests."""

    def __init__(
        self,
        url: str,
        session: requests.Session | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> None:
        super().__init__()
        self.url = url
        self.session = session or make_session()
        self.headers = dict(headers or {})
        self.retries = max(1, retries)
        self.pos = 0
        self.requests_made = 0
        self.size = self._probe()

    # ---------- transport ----------

    def _probe(self) -> int:
        last_error = ""
        for attempt in range(self.retries):
            try:
                response = self.session.get(
                    self.url,
                    headers={**self.headers, "Range": "bytes=0-0"},
                    timeout=(30, READ_TIMEOUT),
                    stream=True,
                )
                self.requests_made += 1
                try:
                    if response.status_code == 206:
                        total = response.headers.get("Content-Range", "").rsplit("/", 1)[-1]
                        if total.isdigit():
                            return int(total)
                    if response.status_code == 200:
                        length = int(response.headers.get("Content-Length") or 0)
                        if length:
                            return length
                    last_error = f"HTTP {response.status_code}"
                finally:
                    response.close()
            except requests.RequestException as exc:
                last_error = str(exc)
            time.sleep(0.7 * (attempt + 1))
        raise RemoteZipError(f"Не удалось открыть архив: {last_error or 'нет ответа'}")

    def _read_range(self, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            return b""
        end = min(end, self.size - 1)
        length = end - start + 1
        last_error = ""
        for attempt in range(self.retries):
            try:
                response = self.session.get(
                    self.url,
                    headers={**self.headers, "Range": f"bytes={start}-{end}"},
                    timeout=(30, READ_TIMEOUT),
                )
                self.requests_made += 1
                if response.status_code == 206:
                    data = response.content
                    if len(data) == length:
                        return data
                    last_error = f"получено {len(data)} из {length} байт"
                elif response.status_code == 200:
                    # CDN ignored Range: only usable for a request starting at zero.
                    data = response.content[start : end + 1]
                    if len(data) == length:
                        return data
                    last_error = "сервер не поддерживает Range"
                else:
                    last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc)
            time.sleep(0.5 * (attempt + 1))
        raise RemoteZipError(f"Ошибка чтения архива: {last_error or 'неизвестная ошибка'}")

    # ---------- io.RawIOBase ----------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self.pos + offset
        elif whence == io.SEEK_END:
            pos = self.size + offset
        else:
            raise ValueError("invalid whence")
        self.pos = max(0, min(pos, self.size))
        return self.pos

    def readinto(self, buffer: bytearray | memoryview) -> int:
        size = len(buffer)
        if size <= 0 or self.pos >= self.size:
            return 0
        data = self._read_range(self.pos, self.pos + size - 1)
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)

    def close(self) -> None:
        try:
            self.session.close()
        finally:
            super().close()


class RemoteZip:
    """Context manager around zipfile for an archive hosted over HTTP."""

    def __init__(self, url: str, session: requests.Session | None = None) -> None:
        self.remote = RangeFile(url, session=session)
        self.buffer = io.BufferedReader(self.remote, buffer_size=DEFAULT_CHUNK)
        self.zip = zipfile.ZipFile(self.buffer)

    def __enter__(self) -> zipfile.ZipFile:
        return self.zip

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.zip.close()
        finally:
            try:
                self.buffer.close()
            finally:
                self.remote.close()

    @property
    def size(self) -> int:
        return self.remote.size


def probe_size(url: str, session: requests.Session | None = None) -> int:
    remote = RangeFile(url, session=session)
    try:
        return remote.size
    finally:
        remote.close()


def parse_config_json(text: str) -> dict[str, Any]:
    """Parse BeamNG config JSON, tolerating a common missing-comma typo."""
    text = text.lstrip("\ufeff")
    try:
        data = json.loads(text)
    except ValueError:
        fixed = re.sub(
            r'(["\d\]\}])\s*\n(\s*"vehicleSelectorSubGroup")',
            r"\1,\n\2",
            text,
        )
        try:
            data = json.loads(fixed)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _is_junk_config(name: str) -> bool:
    lowered = name.lower()
    return (
        not name.strip()
        or "t.me@" in lowered
        or lowered.startswith((".", "*"))
        or lowered in ("creator", "best mods beamng")
    )


def read_vehicle_configs(
    archive: zipfile.ZipFile, prefixes: tuple[str, ...] = ("vehicles/", "mods/")
) -> list[dict[str, Any]]:
    """Return ``[{"name", "group"}]`` for every real config in an archive."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for info in archive.infolist():
        name = info.filename
        if not name.endswith(".json"):
            continue
        if not re.search(r"(^|/)info_config_[^/]+\.json$", name):
            continue
        if not name.startswith(prefixes):
            continue
        try:
            data = parse_config_json(archive.read(info).decode("utf-8-sig", "replace"))
        except (KeyError, OSError, zipfile.BadZipFile):
            continue
        if not data:
            continue
        config_name = str(data.get("Configuration") or "").strip()
        group = str(data.get("vehicleSelectorSubGroup") or "").strip()
        if not config_name:
            stem = name.rsplit("/", 1)[-1]
            stem = stem[len("info_config_") : -len(".json")]
            config_name = stem.replace("_", " ").title()
        if _is_junk_config(config_name):
            continue
        key = (config_name, group)
        if key in seen:
            continue
        seen.add(key)
        found.append({"name": config_name, "group": group})
    return found


def group_configs(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = {}
    for item in configs:
        group = item.get("group") or "Другое"
        groups.setdefault(group, []).append(item["name"])

    def sort_key(name: str) -> tuple[int, str]:
        match = re.match(r"^(\d+)\s", name)
        return (int(match.group(1)) if match else 999, name.lower())

    result = []
    for group_name in sorted(groups, key=sort_key):
        items = sorted(set(groups[group_name]), key=str.lower)
        if group_name == "Другое":
            continue
        result.append({"name": group_name, "items": items})
    return result
