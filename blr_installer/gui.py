from __future__ import annotations

import hashlib
from logging import root
import queue
import sys
import threading
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import ttkbootstrap as ttk

from .config import ICON_FILE, STEAM_OPTIONS, load_zip_url
from .download import (
    discard_download_cache,
    format_size,
    metadata_path,
    read_resume_metadata,
)
from .errors import InstallationPaused
from .service import install_configured
from .state import last_destination, read_job_state, state_directory
from .terminal_ui import launch_game


class InstallerWindow:
    def __init__(self, root: ttk.App) -> None:
        self.root = root
        self.root.title("Blacklight: Retribution Installer")
        if sys.platform == "win32":
            self.root.iconbitmap(ICON_FILE)
        else:
            self.root.iconphoto(True, tk.PhotoImage(file=ICON_FILE))
        self.root.minsize(520, 300)
        self.root.resizable(True, True)
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

        self.keep_download_var = tk.BooleanVar(value=False)
        self.keep_download_checkbox = ttk.Checkbutton(
            frame,
            text="Keep downloaded ZIP for faster reinstalls",
            variable=self.keep_download_var,
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
            text="Have Steam detect the game as Blacklight: Retribution instead of SpaceWar",
            variable=self.owned_app_id_var,
        )
        self.app_id_checkbox.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0),)
        ttk.Label(
            frame,
            text="Requires owning Blacklight: Retribution on Steam.",
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
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=7,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(12, 4),
        )
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=8, column=0, columnspan=2, sticky="ew")

        actions = ttk.Frame(frame)
        actions.grid(row=9, column=0, columnspan=2, sticky="e", pady=(20, 0))
        self.launch_button = ttk.Button(
            actions,
            text="Launch game",
            command=self._launch_game,
            state="disabled",
            bootstyle="success",
        )
        self.launch_button.pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(
            actions,
            text="Install",
            command=self._install_action,
            bootstyle="danger",
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
        self.pause_event.clear()
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
                install_configured(
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
                self.events.put(("complete", None))
            finally:
                self.events.put(("stopped", None))

        self.worker = threading.Thread(target=worker, name="installer-worker", daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_events)

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
            elif event == "error":
                self.progress.stop()
                try:
                    self.state = read_job_state(self.state_path, self.url)
                except ValueError:
                    self.state = None
                if self.state is not None and self.state.get("phase") == "complete":
                    self.status_var.set(
                        "Installation completed, but Steam could not be relaunched."
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
