from __future__ import annotations

import hashlib
import json
import re
import ssl
import threading
import urllib.error
import urllib.request
import zipfile
from email.message import Message
from pathlib import Path
from typing import Callable, Protocol, cast

import certifi

from .config import CHUNK_SIZE
from .errors import InstallationPaused
from .progress import finish_progress, show_progress

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

class DownloadResponse(Protocol):
    headers: Message

    def getcode(self) -> int: ...

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def __enter__(self) -> DownloadResponse: ...

    def __exit__(self, *args: object) -> None: ...


def metadata_path(download_path: Path) -> Path:
    return download_path.with_suffix(download_path.suffix + ".json")


def completed_download_path(download_path: Path) -> Path:
    if download_path.suffix != ".part":
        raise ValueError(f"Expected a partial download path ending in '.part': {download_path}")
    return download_path.with_suffix("")


def cached_download_path(download_path: Path) -> Path:
    completed_path = completed_download_path(download_path)
    return (
        completed_path
        if completed_path.is_file() and zipfile.is_zipfile(completed_path)
        else download_path
    )


def normalize_cached_download(download_path: Path) -> Path:
    completed_path = completed_download_path(download_path)
    if completed_path.is_file() and zipfile.is_zipfile(completed_path):
        download_path.unlink(missing_ok=True)
        metadata_path(download_path).unlink(missing_ok=True)
        return completed_path
    if download_path.is_file() and zipfile.is_zipfile(download_path):
        return _finalize_download(download_path)
    return download_path


def discard_download_cache(download_path: Path) -> None:
    download_path.unlink(missing_ok=True)
    metadata_path(download_path).unlink(missing_ok=True)
    completed_path = completed_download_path(download_path)
    completed_path.unlink(missing_ok=True)
    metadata_path(completed_path).unlink(missing_ok=True)


def _finalize_download(download_path: Path) -> Path:
    if not zipfile.is_zipfile(download_path):
        return download_path

    completed_path = completed_download_path(download_path)
    completed_metadata = metadata_path(completed_path)
    partial_metadata = metadata_path(download_path)
    if download_path != completed_path:
        completed_path.unlink(missing_ok=True)
        download_path.replace(completed_path)
    if partial_metadata.is_file():
        completed_metadata.unlink(missing_ok=True)
        partial_metadata.replace(completed_metadata)
    return completed_path


def _save_resume_metadata(path: Path, metadata: dict[str, str]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(metadata), encoding="utf-8")
    temporary_path.replace(path)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _make_download_request(
    url: str,
    offset: int = 0,
    validator: str = "",
) -> urllib.request.Request:
    headers = {"User-Agent": "SimpleCrossPlatformInstaller/1.0"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        if validator:
            headers["If-Range"] = validator
    return urllib.request.Request(url, headers=headers)


def read_resume_metadata(
    path: Path,
    url: str,
    download_path: Path,
) -> dict[str, str]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if (
            not download_path.is_file()
            or not isinstance(metadata, dict)
            or metadata.get("url_hash") != hashlib.sha256(url.encode("utf-8")).hexdigest()
        ):
            return {}
        return {
            key: value
            for key in ("url_hash", "etag", "last_modified", "total_bytes")
            if isinstance((value := metadata.get(key)), str)
        }
    except (OSError, json.JSONDecodeError):
        return {}


def download_zip(
    url: str,
    download_path: Path,
    on_progress: Callable[[str, int, int | None], None] | None = None,
    pause_event: threading.Event | None = None,
) -> Path:
    completed_path = completed_download_path(download_path)
    if completed_path.is_file():
        if zipfile.is_zipfile(completed_path):
            size = completed_path.stat().st_size
            _report_progress("Download", size, size, on_progress)
            if on_progress is None:
                finish_progress()
            download_path.unlink(missing_ok=True)
            metadata_path(download_path).unlink(missing_ok=True)
            return completed_path
        completed_path.unlink()
        metadata_path(completed_path).unlink(missing_ok=True)

    if download_path.is_file() and zipfile.is_zipfile(download_path):
        return _finalize_download(download_path)

    resume_metadata_path = metadata_path(download_path)
    metadata = read_resume_metadata(resume_metadata_path, url, download_path)
    offset = download_path.stat().st_size if metadata else 0
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    total_value = metadata.get("total_bytes")
    if (
        total_value is not None
        and total_value.isdigit()
        and offset == int(total_value)
        and offset > 0
    ):
        if zipfile.is_zipfile(download_path):
            _report_progress("Download", offset, offset, on_progress)
            if on_progress is None:
                finish_progress()
            return _finalize_download(download_path)
        download_path.unlink(missing_ok=True)
        resume_metadata_path.unlink(missing_ok=True)
        offset = 0
    if offset == 0:
        metadata = {"url_hash": url_hash}

    validator = metadata.get("etag", "") or metadata.get("last_modified", "")
    request = _make_download_request(url, offset, validator)

    try:
        response = cast(
            DownloadResponse,
            urllib.request.urlopen(request, timeout=60, context=SSL_CONTEXT),
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 416:
            raise
        content_range = exc.headers.get("Content-Range", "")
        if content_range == f"bytes */{offset}":
            if zipfile.is_zipfile(download_path):
                _report_progress("Download", offset, offset, on_progress)
                if on_progress is None:
                    finish_progress()
                return _finalize_download(download_path)
        download_path.unlink(missing_ok=True)
        resume_metadata_path.unlink(missing_ok=True)
        metadata = {"url_hash": url_hash}
        offset = 0
        response = cast(
            DownloadResponse,
            urllib.request.urlopen(
                _make_download_request(url),
                timeout=60,
                context=SSL_CONTEXT,
            ),
        )

    with response:
        status = response.getcode()
        content_range = response.headers.get("Content-Range", "")
        range_match: re.Match[str] | None = None
        if status == 206:
            range_match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
        if status == 206 and (
            range_match is None
            or int(range_match.group(1)) != offset
            or (
                offset
                and validator
                and response.headers.get("ETag", response.headers.get("Last-Modified", validator))
                != validator
            )
        ):
            response.close()
            download_path.unlink(missing_ok=True)
            resume_metadata_path.unlink(missing_ok=True)
            metadata = {"url_hash": url_hash}
            response = cast(
                DownloadResponse,
                urllib.request.urlopen(
                    _make_download_request(url),
                    timeout=60,
                    context=SSL_CONTEXT,
                ),
            )
            with response:
                _write_download_response(
                    response,
                    url,
                    download_path,
                    resume_metadata_path,
                    metadata,
                    0,
                    on_progress=on_progress,
                    pause_event=pause_event,
                )
            return _finalize_download(download_path)

        if status == 200 and offset:
            metadata = {"url_hash": url_hash}
        _write_download_response(
            response,
            url,
            download_path,
            resume_metadata_path,
            metadata,
            offset if status == 206 else 0,
            range_match,
            on_progress,
            pause_event,
        )
    return _finalize_download(download_path)


def _write_download_response(
    response: DownloadResponse,
    url: str,
    download_path: Path,
    resume_metadata_path: Path,
    metadata: dict[str, str],
    offset: int,
    range_match: re.Match[str] | None = None,
    on_progress: Callable[[str, int, int | None], None] | None = None,
    pause_event: threading.Event | None = None,
) -> None:
    headers = response.headers
    status = response.getcode()
    etag = headers.get("ETag", "")
    last_modified = headers.get("Last-Modified", "")
    metadata.update({"url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest()})
    if etag:
        metadata["etag"] = etag
    if last_modified:
        metadata["last_modified"] = last_modified
    if status == 206 and range_match is not None:
        range_total = range_match.group(3)
        total = int(range_total) if range_total != "*" else None
    else:
        content_length = headers.get("Content-Length")
        total = offset + int(content_length) if content_length and content_length.isdigit() else None

    if total is not None:
        metadata["total_bytes"] = str(total)
    else:
        metadata.pop("total_bytes", None)
    _save_resume_metadata(resume_metadata_path, metadata)

    downloaded = offset
    _report_progress("Download", downloaded, total, on_progress)
    with download_path.open("ab" if offset else "wb") as output:
        while chunk := response.read(CHUNK_SIZE):
            output.write(chunk)
            downloaded += len(chunk)
            _report_progress("Download", downloaded, total, on_progress)
            if pause_event is not None and pause_event.is_set():
                raise InstallationPaused
    if pause_event is not None and pause_event.is_set():
        raise InstallationPaused
    if on_progress is None:
        finish_progress()

    actual_size = download_path.stat().st_size
    if total is not None and actual_size != total:
        raise OSError(
            f"Download is incomplete ({format_size(actual_size)} of {format_size(total)}). "
            "Run the installer again to resume."
        )


def _report_progress(
    stage: str,
    current: int,
    total: int | None,
    on_progress: Callable[[str, int, int | None], None] | None,
) -> None:
    if on_progress is None:
        show_progress(stage, current, total)
    else:
        on_progress(stage, current, total)
