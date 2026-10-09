from __future__ import annotations

import hashlib
import queue
import threading
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from installer_config import STEAM_OPTIONS, load_zip_url
from installer_download import (
    discard_download_cache,
    format_size,
    metadata_path,
    read_resume_metadata,
)
from installer_errors import InstallationPaused
from installer_service import install_configured
from installer_state import last_destination, read_job_state, state_directory
from installer_ui import launch_game


class InstallerWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Blacklight Retribution Installer")
        self.root.minsize(520, 300)
        self.root.resizable(False, False)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.pause_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.close_when_stopped = False

        try:
            self.url = load_zip_url()
            self.url_hash = hashlib.sha256(self.url.encode("utf-8")).hexdigest()
            self.state_path = state_directory() / f"install-{self.url_hash}.json"
            self.download_path = state_directory() / f"download-{self.url_hash}.zip.part"
            self.state = read_job_state(self.state_path, self.url)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            messagebox.showerror("Installer configuration error", str(exc), parent=root)
            root.destroy()
            return

        self._build_widgets()
        if self.state is not None:
            self.destination_var.set(str(self.state["destination"]))
            self.steam_app_id_var.set(str(self.state["steam_app_id"]))
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

        ttk.Label(frame, text="Steam ownership").grid(row=0, column=0, sticky="w")
        self.steam_app_id_var = tk.StringVar(value="480")
        for index, (label, app_id) in enumerate(STEAM_OPTIONS.values(), start=1):
            ttk.Radiobutton(
                frame,
                text=label,
                variable=self.steam_app_id_var,
                value=app_id,
            ).grid(row=index, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(frame, text="Install folder").grid(
            row=3,
            column=0,
            sticky="w",
            pady=(18, 4),
        )
        self.destination_var = tk.StringVar()
        self.destination_entry = ttk.Entry(
            frame,
            textvariable=self.destination_var,
            width=52,
        )
        self.destination_entry.grid(row=4, column=0, sticky="ew")
        self.browse_button = ttk.Button(
            frame,
            text="Browse...",
            command=self._browse_destination,
        )
        self.browse_button.grid(row=4, column=1, padx=(8, 0))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(18, 4),
        )
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=6, column=0, columnspan=2, sticky="ew")

        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=2, sticky="e", pady=(20, 0))
        self.launch_button = ttk.Button(
            actions,
            text="Launch game",
            command=self._launch_game,
            state="disabled",
        )
        self.launch_button.pack(side="left", padx=(0, 8))
        self.pause_button = ttk.Button(
            actions,
            text="Pause",
            command=self._pause,
            state="disabled",
        )
        self.pause_button.pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(
            actions,
            text="Install",
            command=self._start_install,
        )
        self.start_button.pack(side="left")
        frame.columnconfigure(0, weight=1)

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
            current = (
                self.download_path.stat().st_size
                if self.download_path.is_file()
                else 0
            )
            metadata = read_resume_metadata(
                metadata_path(self.download_path),
                self.url,
                self.download_path,
            )
            total_text = metadata.get("total_bytes")
            total = int(total_text) if total_text and total_text.isdigit() else None
            self.status_var.set(
                f"Download saved: {format_size(current)}"
                + (f" of {format_size(total)}" if total is not None else "")
            )
        elif phase == "extracting" and self.download_path.is_file():
            try:
                with zipfile.ZipFile(self.download_path) as archive:
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

    def _start_install(self) -> None:
        destination_text = self.destination_var.get().strip()
        if not destination_text:
            messagebox.showerror("Install folder required", "Choose an install folder.", parent=self.root)
            return
        destination = Path(destination_text).expanduser()
        steam_app_id = self.steam_app_id_var.get()
        self.pause_event.clear()
        self.start_button.state(["disabled"])
        self.launch_button.state(["disabled"])
        self.pause_button.state(["!disabled"])
        self.browse_button.state(["disabled"])
        self.destination_entry.state(["disabled"])
        self.status_var.set("Preparing installation...")
        self.progress.configure(mode="determinate", value=0)

        def report_progress(stage: str, current: int, total: int | None) -> None:
            self.events.put(("progress", (stage, current, total)))

        def worker() -> None:
            try:
                install_configured(
                    destination,
                    steam_app_id,
                    report_progress,
                    self.pause_event,
                )
            except InstallationPaused:
                self.events.put(("paused", None))
            except Exception as exc:
                self.events.put(("error", exc))
            else:
                self.events.put(("complete", None))
            finally:
                self.events.put(("stopped", None))

        self.worker = threading.Thread(target=worker, name="installer-worker", daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_events)

    def _pause(self) -> None:
        self.pause_event.set()
        self.pause_button.state(["disabled"])
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
                if total is None or total == 0:
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(12)
                    self.status_var.set(f"{stage}: {format_size(current)}")
                else:
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=min(current / total * 100, 100))
                    self.status_var.set(
                        f"{stage}: {min(current / total, 1):.0%} "
                        f"({format_size(current)} of {format_size(total)})"
                    )
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
                keep_download = messagebox.askyesno(
                    "Keep downloaded ZIP?",
                    "Keep the downloaded ZIP for faster reinstallation? "
                    "It will use additional disk space.",
                    parent=self.root,
                )
                try:
                    if keep_download:
                        self.status_var.set(
                            "Installation complete. Downloaded ZIP kept for faster reinstallation."
                        )
                    else:
                        discard_download_cache(self.download_path)
                        self.status_var.set(
                            "Installation complete. Downloaded ZIP removed."
                        )
                except OSError as exc:
                    self.status_var.set(
                        "Installation complete, but the downloaded ZIP could not be removed."
                    )
                    messagebox.showerror(
                        "Could not remove downloaded ZIP",
                        str(exc),
                        parent=self.root,
                    )
            elif event == "error":
                self.progress.stop()
                self.status_var.set("Installation failed.")
                messagebox.showerror("Installation failed", str(payload), parent=self.root)
                self.start_button.state(["!disabled"])
            elif event == "stopped":
                self.pause_button.state(["disabled"])
                self.start_button.state(["!disabled"])
                self.browse_button.state(["!disabled"])
                self.destination_entry.state(["!disabled"])
                if self.close_when_stopped:
                    self.root.destroy()
                    return

        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self._poll_events)

    def _launch_game(self) -> None:
        destination = Path(self.destination_var.get()).expanduser().resolve()
        try:
            launch_game(destination, self.steam_app_id_var.get(), self.status_var.set)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not launch game", str(exc), parent=self.root)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.close_when_stopped = True
            self._pause()
            self.status_var.set("Saving progress before closing...")
        else:
            self.root.destroy()


def run_gui() -> None:
    root = tk.Tk()
    InstallerWindow(root)
    if root.winfo_exists():
        root.mainloop()


if __name__ == "__main__":
    run_gui()
