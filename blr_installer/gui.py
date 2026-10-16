from __future__ import annotations

import hashlib
import math
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import ttkbootstrap as ttk

from .config import (
    GAME_EXECUTABLE,
    ICON_FILE,
    STEAM_OPTIONS,
    load_size_estimates,
    load_zip_url,
)
from .download import (
    cached_download_path,
    discard_download_cache,
    format_size,
    metadata_path,
    read_resume_metadata,
)
from .errors import InstallationPaused
from .service import install_configured
from .steam import launch_steam
from .state import (
    keep_download_preference,
    last_destination,
    read_job_state,
    remember_keep_download,
    state_directory,
)
from .terminal_ui import launch_game


PROGRESS_RATE_SAMPLE_INTERVAL = 0.5


class InstallerWindow:
    def __init__(self, root: ttk.App) -> None:
        self.root = root
        self.root.title("Blacklight: Retribution Installer")
        if sys.platform == "win32":
            self.root.iconbitmap(ICON_FILE)
        else:
            self.root.iconphoto(True, tk.PhotoImage(file=ICON_FILE))
        self.root.minsize(540, 320)
        self.root.resizable(True, True)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.pause_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.close_when_stopped = False
        self.progress_stage: str | None = None
        self.progress_sample_time: float | None = None
        self.progress_sample_bytes = 0
        self.progress_rate: float | None = None

        try:
            self.url = load_zip_url()
            self.url_hash = hashlib.sha256(self.url.encode("utf-8")).hexdigest()
            self.state_path = state_directory() / f"install-{self.url_hash}.json"
            self.download_path = state_directory() / f"download-{self.url_hash}.zip.part"
            self.state = read_job_state(self.state_path, self.url)
            self.saved_keep_download = keep_download_preference()
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            messagebox.showerror("Installer configuration error", str(exc), parent=root)
            root.destroy()
            return

        self._build_widgets()
        if self.state is not None:
            self.destination_var.set(str(self.state["destination"]))
            self.owned_app_id_var.set(
                str(self.state["steam_app_id"]) == STEAM_OPTIONS["1"][1]
            )
            if self.state.get("phase") == "complete":
                self.status_var.set("An installation is already complete.")
                self.start_button.configure(text="Reinstall")
                self.launch_button.state(["!disabled"])
            else:
                self.status_var.set("An interrupted installation can be resumed.")
                self.start_button.configure(text="Continue installation")
                self._show_saved_progress()
        else:
            try:
                self.destination_var.set(str(last_destination()))
            except (OSError, ValueError) as exc:
                messagebox.showerror("Installer settings error", str(exc), parent=root)
                root.destroy()
                return
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)

        self.keep_download_var = tk.BooleanVar(value=self.saved_keep_download)
        self.keep_download_checkbox = ttk.Checkbutton(
            frame,
            text="Keep downloaded ZIP for faster reinstalls",
            variable=self.keep_download_var,
            command=self._keep_download_changed,
        )
        self.keep_download_checkbox.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
        )
        ttk.Label(
            frame,
            text="Uses about 6GB of disk space.",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.owned_app_id_var = tk.BooleanVar(value=False)
        self.app_id_checkbox = ttk.Checkbutton(
            frame,
            text="Have Steam recognize the game as Blacklight: Retribution",
            variable=self.owned_app_id_var,
        )
        self.app_id_checkbox.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(
            frame,
            text="Requires owning Blacklight: Retribution on Steam!",
            wraplength=480,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Label(frame, text="Install folder").grid(
            row=4,
            column=0,
            sticky="w",
            pady=(10, 0),
        )
        self.destination_var = tk.StringVar()
        self.destination_entry = ttk.Entry(
            frame,
            textvariable=self.destination_var,
            width=52,
        )
        self.destination_entry.grid(row=5, column=0, sticky="ew")
        self.browse_button = ttk.Button(
            frame,
            text="Browse...",
            command=self._browse_destination,
            bootstyle="primary",
        )
        self.browse_button.grid(row=5, column=1, padx=(8, 0))

        ttk.Label(
            frame,
            text="The ZIP's blacklightretribution folder will be placed inside this folder.",
            wraplength=480,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status_var, wraplength=520).grid(
            row=7,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(30, 4),
        )
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=8, column=0, columnspan=2, sticky="ew")

        actions = ttk.Frame(frame)
        actions.grid(row=9, column=0, columnspan=2, sticky="wes", pady=(20, 0))
        self.launch_button = ttk.Button(
            actions,
            text="Launch through Steam",
            command=self._launch_game,
            state="disabled",
            bootstyle="success",
        )
        self.launch_button.pack(side="left")
        self.start_button = ttk.Button(
            actions,
            text="Install",
            command=self._install_action,
            bootstyle="danger",
        )
        self.start_button.pack(side="right")
        frame.columnconfigure(0, weight=1)

    def _keep_download_changed(self) -> None:
        keep_download = self.keep_download_var.get()
        try:
            current_state = read_job_state(self.state_path, self.url)
            remember_keep_download(keep_download)
        except (OSError, ValueError) as exc:
            self.keep_download_var.set(self.saved_keep_download)
            messagebox.showerror(
                "Could not save installer settings",
                str(exc),
                parent=self.root,
            )
            return

        self.saved_keep_download = keep_download
        self.state = current_state
        if keep_download or (
            current_state is not None
            and current_state.get("phase") in {"downloading", "extracting"}
        ):
            return
        if not cached_download_path(self.download_path).is_file():
            return

        delete_now = messagebox.askyesno(
            "Delete downloaded ZIP?",
            "Delete the cached ZIP now? Choose No to keep it for the next reinstall.",
            icon="question",
            default=messagebox.NO,
            parent=self.root,
        )
        if delete_now:
            try:
                discard_download_cache(self.download_path)
            except OSError as exc:
                messagebox.showerror(
                    "Could not delete downloaded ZIP",
                    str(exc),
                    parent=self.root,
                )

    def _browse_destination(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            initialdir=self.destination_var.get() or str(last_destination()),
            mustexist=False,
            title="Choose install folder",
        )
        if selected:
            self.destination_var.set(selected)

    def _show_saved_progress(self) -> None:
        if self.state is None:
            return
        phase = self.state.get("phase")
        if phase == "downloading":
            archive_path = cached_download_path(self.download_path)
            current = (
                archive_path.stat().st_size
                if archive_path.is_file()
                else 0
            )
            metadata = read_resume_metadata(
                metadata_path(archive_path),
                self.url,
                archive_path,
            )
            total_text = metadata.get("total_bytes")
            total = int(total_text) if total_text and total_text.isdigit() else None
            self.status_var.set(
                f"Download saved: {format_size(current)}"
                + (f" of {format_size(total)}" if total is not None else "")
            )
        elif phase == "extracting" and cached_download_path(self.download_path).is_file():
            try:
                archive_path = cached_download_path(self.download_path)
                with zipfile.ZipFile(archive_path) as archive:
                    members = archive.infolist()
                    total = sum(member.file_size for member in members if not member.is_dir())
                    progress_map = self.state.get("extraction_progress", {})
                    destination = str(Path(str(self.state["destination"])).resolve())
                    progress = (
                        progress_map.get(destination, {})
                        if isinstance(progress_map, dict)
                        else {}
                    )
                    completed_value = (
                        progress.get("completed_members", [])
                        if isinstance(progress, dict)
                        else []
                    )
                    completed = {
                        index
                        for index in completed_value
                        if isinstance(index, int)
                        and not isinstance(index, bool)
                        and 0 <= index < len(members)
                    } if isinstance(completed_value, list) else set()
                    current = sum(members[index].file_size for index in completed)
                    current_index = progress.get("current_member_index") if isinstance(progress, dict) else None
                    current_bytes = progress.get("current_member_bytes", 0) if isinstance(progress, dict) else 0
                    if (
                        isinstance(current_index, int)
                        and not isinstance(current_index, bool)
                        and 0 <= current_index < len(members)
                        and isinstance(current_bytes, int)
                    ):
                        current += min(current_bytes, members[current_index].file_size)
                    self.status_var.set(
                        f"Extraction saved: {format_size(current)} of {format_size(total)}"
                    )
                    if total:
                        self.progress.configure(value=min(current / total * 100, 100))
            except (OSError, zipfile.BadZipFile):
                self.status_var.set("Saved extraction progress found.")

    def _confirm_preflight(self, destination: Path) -> bool:
        existing_path = destination
        while not existing_path.exists() and existing_path != existing_path.parent:
            existing_path = existing_path.parent
        if existing_path.exists() and not existing_path.is_dir():
            messagebox.showerror(
                "Invalid install folder",
                f"The selected install path passes through a file, not a folder:\n"
                f"{existing_path}",
                parent=self.root,
            )
            return False

        details = [f"Install folder: {destination}"]
        warnings: list[str] = []
        resuming_extraction_here = (
            self.state is not None
            and self.state.get("phase") == "extracting"
            and isinstance(self.state.get("destination"), str)
            and Path(str(self.state["destination"])).expanduser().resolve() == destination
        )
        game_folder = destination / GAME_EXECUTABLE.parent.parent.parent
        if game_folder.exists() and not resuming_extraction_here:
            if (destination / GAME_EXECUTABLE).is_file():
                warnings.append(
                    "An existing game installation was found. Files with matching names "
                    "will be overwritten; other files will be left in place."
                )
            else:
                warnings.append(
                    "An existing blacklightretribution folder was found. Matching files "
                    "may be overwritten; other files will be left in place."
                )

        archive_path = cached_download_path(self.download_path)
        archive_size = archive_path.stat().st_size if archive_path.is_file() else 0
        configured_zip_size, configured_uncompressed_size = load_size_estimates()
        metadata = read_resume_metadata(
            metadata_path(archive_path),
            self.url,
            archive_path,
        )
        total_text = metadata.get("total_bytes")
        server_archive_total = (
            int(total_text) if total_text and total_text.isdigit() else None
        )
        archive_total = server_archive_total or configured_zip_size
        complete_archive = (
            (
                archive_path != self.download_path
                or (
                    self.state is not None
                    and self.state.get("phase") in {"extracting", "complete"}
                )
            )
            and archive_path.is_file()
            and zipfile.is_zipfile(archive_path)
        )
        if (
            server_archive_total is not None
            and archive_size == server_archive_total
            and archive_path.is_file()
            and zipfile.is_zipfile(archive_path)
        ):
            complete_archive = True

        extracted_size = configured_uncompressed_size
        if complete_archive:
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    extracted_size = sum(
                        member.file_size
                        for member in archive.infolist()
                        if not member.is_dir()
                    )
            except (OSError, zipfile.BadZipFile):
                complete_archive = False

        if complete_archive:
            download_needed = 0
        elif archive_total is None:
            download_needed = None
        elif server_archive_total is not None and archive_size >= server_archive_total:
            download_needed = server_archive_total
        else:
            download_needed = max(archive_total - archive_size, 0)
        if download_needed is None:
            details.append("Additional download space: unknown")
        else:
            details.append(f"Additional download space: {format_size(download_needed)}")
        if extracted_size is None:
            details.append("Uncompressed size: unknown")
        else:
            details.append(f"Uncompressed size: about {format_size(extracted_size)}")

        def available_space(path: Path) -> tuple[int, int] | None:
            candidate = path
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            if candidate.is_file():
                candidate = candidate.parent
            try:
                return shutil.disk_usage(candidate).free, candidate.stat().st_dev
            except OSError:
                return None

        download_free = available_space(self.download_path.parent)
        if download_free is None:
            details.append("Free space for download cache: unavailable")
        else:
            details.append(
                f"Free space for download cache: {format_size(download_free[0])}"
            )

        destination_free = available_space(destination)
        if destination_free is None:
            details.append("Free space for installation: unavailable")
        else:
            details.append(
                f"Free space for installation: {format_size(destination_free[0])}"
            )

        if (
            download_free is not None
            and destination_free is not None
            and download_free[1] == destination_free[1]
            and download_needed is not None
            and extracted_size is not None
        ):
            combined_required = download_needed + extracted_size
            if download_free[0] < combined_required:
                if download_needed:
                    warnings.append(
                        "There may not be enough free space for both the download and "
                        "extraction on this drive."
                    )
                else:
                    warnings.append(
                        "There may not be enough free space to extract the archive."
                    )
        else:
            if (
                download_free is not None
                and download_needed is not None
                and download_free[0] < download_needed
            ):
                warnings.append("There may not be enough free space for the download cache.")
            if (
                destination_free is not None
                and extracted_size is not None
                and destination_free[0] < extracted_size
            ):
                warnings.append("There may not be enough free space to extract the archive.")

        if warnings:
            details.extend(("", "Warnings:", *(f"- {warning}" for warning in warnings)))
            details.extend(("", "Continue with this installation?"))
            return messagebox.askyesno(
                "Pre-install check",
                "\n".join(details),
                icon="warning",
                default=messagebox.NO,
                parent=self.root,
            )

        return True

    def _start_install(self) -> None:
        destination_text = self.destination_var.get().strip()
        if not destination_text:
            messagebox.showerror("Install folder required", "Choose an install folder.", parent=self.root)
            return
        try:
            destination = Path(destination_text).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Invalid install folder", str(exc), parent=self.root)
            return
        try:
            self.state = read_job_state(self.state_path, self.url)
            continue_install = self._confirm_preflight(destination)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            messagebox.showerror("Could not check install folder", str(exc), parent=self.root)
            return
        if not continue_install:
            return
        self.pause_event.clear()
        self.progress_stage = None
        self.progress_sample_time = None
        self.progress_sample_bytes = 0
        self.progress_rate = None
        self.start_button.configure(text="Pause")
        self.start_button.state(["!disabled"])
        self.launch_button.state(["disabled"])
        self.browse_button.state(["disabled"])
        self.destination_entry.state(["disabled"])
        self.app_id_checkbox.state(["disabled"])
        self.keep_download_checkbox.state(["disabled"])
        self.status_var.set("Preparing installation...")
        self.progress.configure(mode="determinate", value=0)
        steam_app_id = (
            STEAM_OPTIONS["1"][1]
            if self.owned_app_id_var.get()
            else STEAM_OPTIONS["2"][1]
        )

        def report_progress(stage: str, current: int, total: int | None) -> None:
            self.events.put(("progress", (stage, current, total)))

        def prompt_to_close_steam() -> bool:
            response = threading.Event()
            answer: list[bool] = []
            self.events.put(("steam_prompt", (response, answer)))
            response.wait()
            return answer[0]

        def choose_steam_user(users: list[tuple[Path, str]]) -> Path | None:
            response = threading.Event()
            answer: list[Path | None] = []
            self.events.put(("steam_user_prompt", (users, response, answer)))
            response.wait()
            return answer[0]

        def worker() -> None:
            try:
                steam_root = install_configured(
                    destination,
                    steam_app_id,
                    report_progress,
                    self.pause_event,
                    prompt_to_close_steam,
                    choose_steam_user,
                )
            except InstallationPaused:
                self.events.put(("paused", None))
            except Exception as exc:
                self.events.put(("error", exc))
            else:
                self.events.put(("complete", steam_root))
            finally:
                self.events.put(("stopped", None))

        self.worker = threading.Thread(target=worker, name="installer-worker", daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_events)

    def _update_progress_rate(self, stage: str, current: int) -> float | None:
        now = time.monotonic()
        if stage != self.progress_stage or current < self.progress_sample_bytes:
            self.progress_stage = stage
            self.progress_sample_time = now
            self.progress_sample_bytes = current
            self.progress_rate = None
            return None

        if self.progress_sample_time is not None:
            elapsed = now - self.progress_sample_time
            transferred = current - self.progress_sample_bytes
            if elapsed >= PROGRESS_RATE_SAMPLE_INTERVAL and transferred > 0:
                sample_rate = transferred / elapsed
                self.progress_rate = (
                    sample_rate
                    if self.progress_rate is None
                    else self.progress_rate * 0.75 + sample_rate * 0.25
                )
                self.progress_sample_time = now
                self.progress_sample_bytes = current
        return self.progress_rate

    @staticmethod
    def _format_eta(seconds: int) -> str:
        hours, remainder = divmod(max(seconds, 0), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def _install_action(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self._pause()
        else:
            self._start_install()

    def _pause(self) -> None:
        self.pause_event.set()
        self.start_button.state(["disabled"])
        self.status_var.set("Pausing after the current download/extraction chunk...")

    def _poll_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "progress":
                stage, current, total = payload  # type: ignore[misc]
                stage = str(stage)
                current = int(current)
                total = int(total) if total is not None else None
                rate = self._update_progress_rate(stage, current)
                if total is None or total == 0:
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(12)
                    progress_text = f"{stage}: {format_size(current)}"
                else:
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=min(current / total * 100, 100))
                    progress_text = (
                        f"{stage}: {min(current / total, 1):.0%} "
                        f"({format_size(current)} of {format_size(total)})"
                    )
                if rate is not None:
                    progress_text += f" | {format_size(int(rate))}/s"
                    if total is not None and total > current:
                        eta_seconds = math.ceil((total - current) / rate)
                        progress_text += (
                            f" | about {self._format_eta(eta_seconds)} left"
                        )
                self.status_var.set(progress_text)
            elif event == "steam_prompt":
                response, answer = payload  # type: ignore[misc]
                self.start_button.state(["disabled"])
                answer.append(
                    messagebox.askokcancel(
                        "Close Steam",
                        "The download and extraction are complete. Steam must be fully "
                        "closed now so the installer can add the library shortcut. "
                        "Exit Steam, including its tray process, then choose OK. "
                        "Choose Cancel to pause the installation.",
                        parent=self.root,
                    )
                )
                response.set()
            elif event == "steam_user_prompt":
                users, response, answer = payload  # type: ignore[misc]
                choices = "\n".join(
                    f"{index}. {label}"
                    for index, (_, label) in enumerate(users, start=1)
                )
                self.start_button.state(["disabled"])
                selection = simpledialog.askinteger(
                    "Select Steam user",
                    "Choose the Steam account whose library should receive the "
                    f"BLRevive shortcut:\n\n{choices}\n\nEnter a number:",
                    minvalue=1,
                    maxvalue=len(users),
                    parent=self.root,
                )
                answer.append(users[selection - 1][0] if selection is not None else None)
                response.set()
            elif event == "paused":
                self.progress.stop()
                self.status_var.set("Paused. Progress is saved; press Continue to resume.")
                self.start_button.configure(text="Continue installation")
                self.start_button.state(["!disabled"])
            elif event == "complete":
                self.progress.stop()
                self.progress.configure(mode="determinate", value=100)
                self.start_button.configure(text="Reinstall")
                self.launch_button.state(["!disabled"])
                try:
                    if self.keep_download_var.get():
                        self.status_var.set(
                            "Installation complete. Downloaded ZIP kept for faster reinstallation."
                        )
                    else:
                        discard_download_cache(self.download_path)
                        self.status_var.set("Installation complete. Downloaded ZIP removed.")
                except OSError as exc:
                    self.status_var.set(
                        "Installation complete, but the downloaded ZIP could not be removed."
                    )
                    messagebox.showerror(
                        "Could not remove downloaded ZIP",
                        str(exc),
                        parent=self.root,
                    )
                if messagebox.askyesno(
                    "Installation complete",
                    "Installation is complete. Would you like to launch Steam now?",
                    default=messagebox.NO,
                    parent=self.root,
                ):
                    try:
                        launch_steam(Path(payload))
                    except (OSError, RuntimeError) as exc:
                        self.status_var.set(
                            "Installation complete, but Steam could not be launched."
                        )
                        messagebox.showerror(
                            "Could not launch Steam",
                            str(exc),
                            parent=self.root,
                        )
            elif event == "error":
                self.progress.stop()
                try:
                    self.state = read_job_state(self.state_path, self.url)
                except ValueError:
                    self.state = None
                if self.state is not None and self.state.get("phase") == "complete":
                    self.status_var.set(
                        "Installation completed, but Steam setup reported an error."
                    )
                    self.start_button.configure(text="Reinstall")
                    self.launch_button.state(["!disabled"])
                else:
                    self.status_var.set("Installation failed.")
                    self.start_button.configure(
                        text="Continue installation" if self.state is not None else "Install"
                    )
                messagebox.showerror("Installation failed", str(payload), parent=self.root)
                self.start_button.state(["!disabled"])
            elif event == "stopped":
                self.start_button.state(["!disabled"])
                self.browse_button.state(["!disabled"])
                self.destination_entry.state(["!disabled"])
                self.app_id_checkbox.state(["!disabled"])
                self.keep_download_checkbox.state(["!disabled"])
                if self.close_when_stopped:
                    self.root.destroy()
                    return

        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self._poll_events)

    def _launch_game(self) -> None:
        destination = Path(self.destination_var.get()).expanduser().resolve()
        try:
            steam_app_id = (
                STEAM_OPTIONS["1"][1]
                if self.owned_app_id_var.get()
                else STEAM_OPTIONS["2"][1]
            )
            if launch_game(destination, steam_app_id, self.status_var.set):
                self.root.destroy()
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Could not launch game", str(exc), parent=self.root)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.close_when_stopped = True
            self._pause()
            self.status_var.set("Saving progress before closing...")
        else:
            self.root.destroy()


def run_gui() -> None:
    root = ttk.App(theme="bootstrap-dark")
    InstallerWindow(root)
    if root.winfo_exists():
        root.mainloop()


if __name__ == "__main__":
    run_gui()
