from __future__ import annotations

import hashlib
import stat
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from installer_config import CHUNK_SIZE
from installer_errors import InstallationPaused


def _target_for_member(root: Path, member: zipfile.ZipInfo) -> Path:
    normalized_name = member.filename.replace("\\", "/")
    member_path = PurePosixPath(normalized_name)
    if (
        member_path.is_absolute()
        or not member_path.parts
        or any(part in {".", ".."} for part in member_path.parts)
        or ":" in member_path.parts[0]
    ):
        raise ValueError(f"Unsafe path in ZIP archive: {member.filename}")

    target = root.joinpath(*member_path.parts)
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Unsafe path in ZIP archive: {member.filename}") from exc

    unix_mode = member.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise ValueError(f"ZIP archive contains an unsupported symbolic link: {member.filename}")
    return target


def _archive_signature(members: list[zipfile.ZipInfo]) -> str:
    signature_content = "\n".join(
        f"{member.filename}\0{member.file_size}\0{member.CRC}\0{member.compress_size}"
        for member in members
    )
    return hashlib.sha256(signature_content.encode("utf-8")).hexdigest()


def safe_extract(
    archive: zipfile.ZipFile,
    destination: Path,
    on_progress: Callable[[int, int], None] | None = None,
    progress_state: dict[str, object] | None = None,
    save_progress: Callable[[], None] | None = None,
    pause_event: threading.Event | None = None,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    members = archive.infolist()
    targets = [_target_for_member(root, member) for member in members]
    total_size = sum(member.file_size for member in members if not member.is_dir())
    completed_value = progress_state.get("completed_members", []) if progress_state else []
    completed = {
        value
        for value in completed_value
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(members)
    } if isinstance(completed_value, list) else set()
    current_index = progress_state.get("current_member_index") if progress_state else None
    current_bytes = progress_state.get("current_member_bytes", 0) if progress_state else 0
    if not isinstance(current_index, int) or isinstance(current_index, bool):
        current_index = None
    if not isinstance(current_bytes, int) or isinstance(current_bytes, bool) or current_bytes < 0:
        current_bytes = 0
    if (
        current_index is None
        or not 0 <= current_index < len(members)
        or members[current_index].is_dir()
    ):
        current_index = None
        current_bytes = 0
    elif current_bytes > members[current_index].file_size:
        current_bytes = 0
    if current_index in completed:
        current_index = None
        current_bytes = 0

    if progress_state is not None:
        signature = _archive_signature(members)
        if progress_state.get("archive_signature") != signature:
            completed.clear()
            current_index = None
            current_bytes = 0
        progress_state["archive_signature"] = signature

    completed = {
        index
        for index in completed
        if not members[index].is_dir()
        and targets[index].is_file()
        and targets[index].stat().st_size == members[index].file_size
    }
    completed_size = sum(
        members[index].file_size
        for index in completed
        if not members[index].is_dir()
    )
    if on_progress is not None:
        progress_index = current_index if current_index is not None else -1
        progress_bytes = (
            min(current_bytes, members[progress_index].file_size)
            if progress_index >= 0
            else 0
        )
        on_progress(completed_size + progress_bytes, total_size)

    for index, member in enumerate(members):
        target = targets[index]
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        if index in completed:
            if target.is_file() and target.stat().st_size == member.file_size:
                continue
            completed.remove(index)
            completed_size -= member.file_size

        target.parent.mkdir(parents=True, exist_ok=True)
        resume_bytes = current_bytes if current_index == index else 0
        if resume_bytes > member.file_size or not target.is_file():
            resume_bytes = 0
        elif target.stat().st_size < resume_bytes:
            resume_bytes = 0

        if resume_bytes:
            with target.open("r+b") as output:
                output.truncate(resume_bytes)
        with archive.open(member) as source:
            skipped = 0
            while skipped < resume_bytes:
                chunk = source.read(min(CHUNK_SIZE, resume_bytes - skipped))
                if not chunk:
                    raise zipfile.BadZipFile(f"Could not resume extracting {member.filename}")
                skipped += len(chunk)

            if progress_state is not None:
                progress_state["current_member_index"] = index
                progress_state["current_member_bytes"] = resume_bytes
            if on_progress is not None:
                on_progress(completed_size + resume_bytes, total_size)
            if save_progress is not None:
                save_progress()
            if pause_event is not None and pause_event.is_set():
                raise InstallationPaused

            with target.open("ab" if resume_bytes else "wb") as output:
                extracted_from_member = resume_bytes
                while chunk := source.read(CHUNK_SIZE):
                    output.write(chunk)
                    extracted_from_member += len(chunk)
                    if progress_state is not None:
                        progress_state["current_member_index"] = index
                        progress_state["current_member_bytes"] = extracted_from_member
                    if on_progress is not None:
                        on_progress(completed_size + extracted_from_member, total_size)
                    if save_progress is not None:
                        save_progress()
                    if pause_event is not None and pause_event.is_set():
                        raise InstallationPaused

        if target.stat().st_size != member.file_size:
            raise zipfile.BadZipFile(f"Extracted file size mismatch: {member.filename}")
        completed.add(index)
        completed_size += member.file_size
        current_index = None
        current_bytes = 0
        if progress_state is not None:
            progress_state["completed_members"] = sorted(completed)
            progress_state["current_member_index"] = None
            progress_state["current_member_bytes"] = 0
        if save_progress is not None:
            save_progress()

    if progress_state is not None:
        progress_state["completed_members"] = sorted(completed)
        progress_state["current_member_index"] = None
        progress_state["current_member_bytes"] = 0

    if on_progress is not None and total_size == 0:
        on_progress(total_size, total_size)
