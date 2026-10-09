import hashlib
import io
import json
import tempfile
import threading
import unittest
import zipfile
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

import installer
import installer_download
import installer_errors
import installer_extract
import installer_service
import installer_state
import installer_ui
from installer_config import DEFAULT_LAUNCH_ARGS, GAME_EXECUTABLE, TEXT_FILE
from installer_extract import safe_extract


class FakeResponse:
    def __init__(self, body: bytes, status: int, headers: dict[str, str], fail_after: bool = False) -> None:
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value
        self.fail_after = fail_after
        self.read_once = False

    def getcode(self) -> int:
        return self.status

    def read(self, amount: int = -1) -> bytes:
        if self.fail_after and self.read_once:
            raise OSError("Connection interrupted")
        self.read_once = True
        return self.body.read(amount)

    def close(self) -> None:
        self.body.close()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class SafeExtractTests(unittest.TestCase):
    def test_extracts_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "package.zip"
            destination = root / "install"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("blacklightretribution/Binaries/Win32/game.exe", b"game")

            with zipfile.ZipFile(archive_path) as archive:
                safe_extract(archive, destination)

            self.assertEqual(
                (destination / "blacklightretribution" / "Binaries" / "Win32" / "game.exe").read_bytes(),
                b"game",
            )

    def test_rejects_parent_directory_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "package.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", b"unsafe")

            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaisesRegex(ValueError, "Unsafe path"):
                    safe_extract(archive, root / "install")

    def test_text_file_location(self) -> None:
        self.assertEqual(
            TEXT_FILE,
            Path("blacklightretribution") / "Binaries" / "Win32" / "steam_appid.txt",
        )

    def test_saved_state_normalizes_numeric_steam_app_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "install.json"
            url = "https://example.test/game.zip"
            path.write_text(
                json.dumps(
                    {
                        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                        "destination": str(Path(temp_dir) / "install"),
                        "steam_app_id": 480,
                        "phase": "complete",
                    }
                ),
                encoding="utf-8",
            )

            state = installer_state.read_job_state(path, url)

            self.assertIsNotNone(state)
            self.assertEqual(state["steam_app_id"], "480")

    def test_install_writes_selected_app_id(self) -> None:
        for app_id in ("209870", "480"):
            with self.subTest(app_id=app_id), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                destination = root / "install"

                def create_test_zip(_: str, download_path: Path, *_args: object) -> None:
                    with zipfile.ZipFile(download_path, "w") as archive:
                        archive.writestr("blacklightretribution/Binaries/Win32/game.exe", b"game")

                with (
                    patch.object(installer, "_state_directory", return_value=root / "state"),
                    patch.object(installer_state, "state_directory", return_value=root / "state"),
                    patch.object(installer, "load_zip_url", return_value="https://example.test/game.zip"),
                    patch.object(installer, "choose_steam_option", return_value=app_id),
                    patch.object(installer, "choose_destination", return_value=destination),
                    patch.object(installer_service, "download_zip", side_effect=create_test_zip),
                    patch.object(installer, "_prompt_keep_download", return_value=True),
                    patch.object(installer, "_prompt_for_completed_action", return_value="4"),
                ):
                    installer.install()

                self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), app_id)
                url_hash = hashlib.sha256(
                    "https://example.test/game.zip".encode("utf-8")
                ).hexdigest()
                saved_state = json.loads(
                    (root / "state" / f"install-{url_hash}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(saved_state["phase"], "complete")
                self.assertEqual(saved_state["destination"], str(destination.resolve()))
                self.assertTrue((root / "state" / f"download-{url_hash}.zip.part").is_file())

    def test_completed_install_prompts_to_launch_and_keeps_completion_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_directory = root / "state"
            destination = root / "install"
            url = "https://example.test/game.zip"
            url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
            state_path = state_directory / f"install-{url_hash}.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "url_hash": url_hash,
                        "destination": str(destination),
                        "steam_app_id": "480",
                        "phase": "complete",
                        "completed_members": [],
                        "current_member_index": None,
                        "current_member_bytes": 0,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(installer, "_state_directory", return_value=state_directory),
                patch.object(installer_state, "state_directory", return_value=state_directory),
                patch.object(installer, "load_zip_url", return_value=url),
                patch.object(installer, "_prompt_for_completed_action", return_value="1"),
                patch.object(installer, "_launch_game") as launch_game,
            ):
                installer.install()

            launch_game.assert_called_once_with(destination.resolve(), "480")
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["phase"], "complete")

    def test_launch_game_uses_configured_executable_and_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "install"
            executable = destination / GAME_EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"placeholder")

            with (
                patch.object(installer_ui.sys, "platform", "win32"),
                patch.object(installer_ui.subprocess, "Popen") as popen,
            ):
                messages: list[str] = []
                installer_ui.launch_game(destination, "209870", on_message=messages.append)

            popen.assert_called_once_with(
                [str(executable), *DEFAULT_LAUNCH_ARGS],
                cwd=executable.parent,
            )
            self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), "209870")
            self.assertEqual(messages, ["Launching FoxGame-win32-Shipping.exe..."])

    def test_launch_game_uses_wine_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "install"
            executable = destination / GAME_EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"placeholder")

            with (
                patch.object(installer_ui.sys, "platform", "linux"),
                patch.object(installer_ui.subprocess, "Popen") as popen,
            ):
                installer_ui.launch_game(destination, "480")

            popen.assert_called_once_with(
                ["wine", str(executable), *DEFAULT_LAUNCH_ARGS],
                cwd=executable.parent,
            )
            self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), "480")

    def test_launch_game_ignores_other_executables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "install"
            game_directory = destination / GAME_EXECUTABLE.parent
            game_directory.mkdir(parents=True)
            (game_directory / "SomeOther.exe").write_bytes(b"placeholder")
            messages: list[str] = []

            with patch.object(installer_ui.subprocess, "Popen") as popen:
                installer_ui.launch_game(destination, "480", on_message=messages.append)

            popen.assert_not_called()
            self.assertEqual(len(messages), 1)
            self.assertIn(GAME_EXECUTABLE.name, messages[0])
            self.assertFalse((destination / TEXT_FILE).exists())

    def test_launch_game_rejects_invalid_steam_app_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "install"
            executable = destination / GAME_EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"placeholder")

            with (
                patch.object(installer_ui.subprocess, "Popen") as popen,
                self.assertRaisesRegex(ValueError, "valid Steam ownership"),
            ):
                installer_ui.launch_game(destination, "12345")

            popen.assert_not_called()

    def test_prompt_keep_download_accepts_yes_and_defaults_to_no(self) -> None:
        with patch("builtins.input", side_effect=("invalid", "yes")):
            self.assertTrue(installer_ui.prompt_keep_download())
        with patch("builtins.input", return_value=""):
            self.assertFalse(installer_ui.prompt_keep_download())

    def test_completed_install_can_be_reinstalled_with_saved_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_directory = root / "state"
            destination = root / "install"
            url = "https://example.test/game.zip"
            url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
            state_path = state_directory / f"install-{url_hash}.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "url_hash": url_hash,
                        "destination": str(destination),
                        "steam_app_id": "480",
                        "phase": "complete",
                        "completed_members": [],
                        "current_member_index": None,
                        "current_member_bytes": 0,
                    }
                ),
                encoding="utf-8",
            )
            actions = iter(("2", "4"))

            def fake_run_install(
                _: str,
                __: Path,
                ___: Path,
                saved_state: dict[str, object],
            ) -> None:
                self.assertEqual(saved_state["phase"], "downloading")
                saved_state["phase"] = "complete"
                installer_state.atomic_write_json(state_path, saved_state)

            with (
                patch.object(installer, "_state_directory", return_value=state_directory),
                patch.object(installer_state, "state_directory", return_value=state_directory),
                patch.object(installer, "load_zip_url", return_value=url),
                patch.object(installer, "_prompt_keep_download", return_value=False),
                patch.object(installer, "_prompt_for_completed_action", side_effect=lambda: next(actions)),
                patch.object(installer, "_run_install", side_effect=fake_run_install) as run_install,
            ):
                installer.install()

            run_install.assert_called_once()
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["phase"], "complete")

    def test_interrupted_download_resumes_from_saved_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "package.zip.part"
            metadata_path = installer_download.metadata_path(download_path)
            url = "https://example.test/package.zip"
            interrupted_response = FakeResponse(
                b"abc",
                200,
                {"Content-Length": "6", "ETag": '"version-1"'},
                fail_after=True,
            )
            with patch.object(installer_download.urllib.request, "urlopen", return_value=interrupted_response):
                with self.assertRaisesRegex(OSError, "Connection interrupted"):
                    installer_download.download_zip(url, download_path)

            self.assertEqual(download_path.read_bytes(), b"abc")
            self.assertTrue(metadata_path.exists())
            resumed_response = FakeResponse(
                b"def",
                206,
                {
                    "Content-Range": "bytes 3-5/6",
                    "Content-Length": "3",
                    "ETag": '"version-1"',
                },
            )
            with patch.object(
                installer_download.urllib.request,
                "urlopen",
                return_value=resumed_response,
            ) as urlopen:
                installer_download.download_zip(url, download_path)

            request = urlopen.call_args.args[0]
            self.assertIsInstance(request, Request)
            self.assertEqual(request.get_header("Range"), "bytes=3-")
            self.assertEqual(request.get_header("If-range"), '"version-1"')
            self.assertEqual(download_path.read_bytes(), b"abcdef")

    def test_download_pause_preserves_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "package.zip.part"
            url = "https://example.test/package.zip"
            pause_event = threading.Event()
            response = FakeResponse(
                b"partial",
                200,
                {"Content-Length": "100", "ETag": '"version-1"'},
            )

            def request_pause(stage: str, current: int, total: int | None) -> None:
                if stage == "Download" and current:
                    pause_event.set()

            with patch.object(installer_download.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(installer_errors.InstallationPaused):
                    installer_download.download_zip(
                        url,
                        download_path,
                        request_pause,
                        pause_event,
                    )

            self.assertEqual(download_path.read_bytes(), b"partial")
            self.assertTrue(installer_download.metadata_path(download_path).exists())

    def test_server_ignoring_range_restarts_download_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "package.zip.part"
            metadata_path = installer_download.metadata_path(download_path)
            url = "https://example.test/package.zip"
            download_path.write_bytes(b"old")
            metadata_path.write_text(
                json.dumps(
                    {
                        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                        "etag": '"old-version"',
                    }
                ),
                encoding="utf-8",
            )
            response = FakeResponse(
                b"complete",
                200,
                {"Content-Length": "8", "ETag": '"new-version"'},
            )

            with patch.object(installer_download.urllib.request, "urlopen", return_value=response):
                installer_download.download_zip(url, download_path)

            self.assertEqual(download_path.read_bytes(), b"complete")

    def test_complete_download_is_reused_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "package.zip.part"
            url = "https://example.test/package.zip"
            with zipfile.ZipFile(download_path, "w") as archive:
                archive.writestr("game.bin", b"game")
            size = download_path.stat().st_size
            installer_download.metadata_path(download_path).write_text(
                json.dumps(
                    {
                        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                        "total_bytes": str(size),
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                installer_download.urllib.request,
                "urlopen",
                side_effect=AssertionError("complete ZIP should be reused"),
            ):
                installer_download.download_zip(url, download_path)

    def test_discard_download_cache_removes_archive_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "package.zip.part"
            resume_metadata_path = installer_download.metadata_path(download_path)
            download_path.write_bytes(b"archive")
            resume_metadata_path.write_text("{}", encoding="utf-8")

            installer_download.discard_download_cache(download_path)

            self.assertFalse(download_path.exists())
            self.assertFalse(resume_metadata_path.exists())

    def test_extraction_reports_byte_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "package.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("game.bin", b"some game data")
            progress: list[tuple[int, int]] = []

            with zipfile.ZipFile(archive_path) as archive:
                safe_extract(
                    archive,
                    root / "install",
                    lambda current, total: progress.append((current, total)),
                )

            self.assertTrue(progress)
            self.assertEqual(progress[-1], (len(b"some game data"), len(b"some game data")))

    def test_extraction_pause_saves_current_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "package.zip"
            content = b"b" * (installer_download.CHUNK_SIZE * 2)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("game.bin", content)
            pause_event = threading.Event()
            state: dict[str, object] = {
                "completed_members": [],
                "current_member_index": None,
                "current_member_bytes": 0,
            }
            destination = root / "install"

            def request_pause(current: int, total: int) -> None:
                if current:
                    pause_event.set()

            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(installer_errors.InstallationPaused):
                    installer_extract.safe_extract(
                        archive,
                        destination,
                        request_pause,
                        state,
                        lambda: None,
                        pause_event,
                    )

            self.assertEqual(
                (destination / "game.bin").stat().st_size,
                installer_download.CHUNK_SIZE,
            )
            self.assertEqual(state["current_member_bytes"], installer_download.CHUNK_SIZE)

    def test_extraction_resumes_from_saved_partial_file_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "package.zip"
            content = b"a" * (installer_download.CHUNK_SIZE * 2 + 17)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("game.bin", content)

            state: dict[str, object] = {
                "completed_members": [],
                "current_member_index": None,
                "current_member_bytes": 0,
            }
            checkpoint_path = root / "progress.json"

            def save_progress() -> None:
                checkpoint_path.write_text(json.dumps(state), encoding="utf-8")
                if state["current_member_bytes"] == installer_download.CHUNK_SIZE:
                    raise KeyboardInterrupt

            destination = root / "install"
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(KeyboardInterrupt):
                    safe_extract(
                        archive,
                        destination,
                        progress_state=state,
                        save_progress=save_progress,
                    )

            self.assertEqual(
                (destination / "game.bin").stat().st_size,
                installer_download.CHUNK_SIZE,
            )
            saved_state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            resumed_progress: list[tuple[int, int]] = []
            with zipfile.ZipFile(archive_path) as archive:
                safe_extract(
                    archive,
                    destination,
                    lambda current, total: resumed_progress.append((current, total)),
                    saved_state,
                    lambda: checkpoint_path.write_text(
                        json.dumps(saved_state),
                        encoding="utf-8",
                    ),
                )

            self.assertEqual((destination / "game.bin").read_bytes(), content)
            self.assertEqual(
                resumed_progress[0],
                (installer_download.CHUNK_SIZE, len(content)),
            )

    def test_extraction_checkpoint_is_restored_when_switching_back_to_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "package.zip"
            content = b"a" * (installer_download.CHUNK_SIZE * 2 + 17)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("game.bin", content)

            destination_a = root / "install-a"
            destination_b = root / "install-b"
            state: dict[str, object] = {
                "destination": str(destination_a.resolve()),
                "completed_members": [],
                "current_member_index": None,
                "current_member_bytes": 0,
            }

            def interrupt_after_first_chunk(checkpoint: dict[str, object]) -> None:
                def save_progress() -> None:
                    if checkpoint["current_member_bytes"] == installer_download.CHUNK_SIZE:
                        raise KeyboardInterrupt

                with zipfile.ZipFile(archive_path) as archive:
                    with self.assertRaises(KeyboardInterrupt):
                        safe_extract(
                            archive,
                            Path(str(state["destination"])),
                            progress_state=checkpoint,
                            save_progress=save_progress,
                        )

            progress_a = installer_state.extraction_progress_for_destination(state, destination_a)
            interrupt_after_first_chunk(progress_a)
            state["destination"] = str(destination_b.resolve())
            progress_b = installer_state.extraction_progress_for_destination(state, destination_b)
            interrupt_after_first_chunk(progress_b)

            state["destination"] = str(destination_a.resolve())
            restored_progress_a = installer_state.extraction_progress_for_destination(
                state,
                destination_a,
            )
            with zipfile.ZipFile(archive_path) as archive:
                safe_extract(
                    archive,
                    destination_a,
                    progress_state=restored_progress_a,
                )

            self.assertEqual((destination_a / "game.bin").read_bytes(), content)
            self.assertEqual(restored_progress_a["completed_members"], [0])
            self.assertEqual(
                progress_b["current_member_bytes"],
                installer_download.CHUNK_SIZE,
            )

    def test_continue_prompt_reuses_saved_destination_and_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_directory = root / "state"
            destination = root / "remembered-install"
            url = "https://example.test/game.zip"
            url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
            state_path = state_directory / f"install-{url_hash}.json"
            download_path = state_directory / f"download-{url_hash}.zip.part"
            download_path.parent.mkdir(parents=True)
            with zipfile.ZipFile(download_path, "w") as archive:
                archive.writestr(
                    "blacklightretribution/Binaries/Win32/game.exe",
                    b"game",
                )
            state_path.write_text(
                json.dumps(
                    {
                        "url_hash": url_hash,
                        "destination": str(destination),
                        "steam_app_id": "480",
                        "phase": "extracting",
                        "completed_members": [],
                        "current_member_index": None,
                        "current_member_bytes": 0,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(installer, "_state_directory", return_value=state_directory),
                patch.object(installer_state, "state_directory", return_value=state_directory),
                patch.object(installer, "load_zip_url", return_value=url),
                patch("builtins.input", return_value="1"),
                patch.object(
                    installer_service,
                    "download_zip",
                    side_effect=AssertionError("should reuse ZIP"),
                ),
                patch.object(installer, "_prompt_keep_download", return_value=False),
                patch.object(installer, "_prompt_for_completed_action", return_value="4"),
            ):
                installer.install()

            self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), "480")
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["phase"], "complete")
            settings = json.loads((state_directory / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["last_destination"], str(destination.resolve()))


if __name__ == "__main__":
    unittest.main()
