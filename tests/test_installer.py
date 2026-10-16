import hashlib
import io
import json
from contextlib import redirect_stdout
import tempfile
import threading
import unittest
import zipfile
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request

from blr_installer import cli as installer
from blr_installer import config as installer_config
from blr_installer import download as installer_download
from blr_installer import errors as installer_errors
from blr_installer import extract as installer_extract
from blr_installer import gui as installer_gui
from blr_installer import service as installer_service
from blr_installer import state as installer_state
from blr_installer import steam as blrevive_steam_setup
from blr_installer import terminal_ui as installer_ui
from blr_installer.config import GAME_EXECUTABLE, TEXT_FILE
from blr_installer.extract import safe_extract


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
    def test_default_destination_uses_platform_install_location(self) -> None:
        with (
            patch.object(installer_state.sys, "platform", "win32"),
            patch.dict(
                installer_state.os.environ,
                {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"},
                clear=True,
            ),
        ):
            self.assertEqual(
                installer_state.default_destination(),
                Path(r"C:\Users\test\AppData\Local\BLRevive"),
            )

        with patch.object(installer_state.sys, "platform", "linux"):
            with patch.object(Path, "home", return_value=Path("/home/test")):
                self.assertEqual(
                    installer_state.default_destination(),
                    Path("/home/test/Games"),
                )

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

    def test_saved_keep_download_preference_survives_destination_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(installer_state, "state_directory", return_value=root):
                installer_state.remember_keep_download(True)
                installer_state.remember_destination(root / "install")

                self.assertTrue(installer_state.keep_download_preference())
                settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["last_destination"], str((root / "install").resolve()))
                self.assertTrue(settings["keep_download"])

    def test_install_writes_selected_app_id(self) -> None:
        for app_id in ("209870", "480"):
            with self.subTest(app_id=app_id), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                destination = root / "install"

                def create_test_zip(_: str, download_path: Path, *_args: object) -> Path:
                    with zipfile.ZipFile(download_path, "w") as archive:
                        archive.writestr("blacklightretribution/Binaries/Win32/game.exe", b"game")
                    completed_path = download_path.with_suffix("")
                    download_path.replace(completed_path)
                    return completed_path

                with (
                    patch.object(installer, "_state_directory", return_value=root / "state"),
                    patch.object(installer_state, "state_directory", return_value=root / "state"),
                    patch.object(installer, "load_zip_url", return_value="https://example.test/game.zip"),
                    patch.object(installer, "choose_steam_option", return_value=app_id),
                    patch.object(installer, "choose_destination", return_value=destination),
                    patch.object(installer_service, "download_zip", side_effect=create_test_zip),
                    patch.object(
                        installer_service,
                        "configure_steam_shortcut",
                        return_value=(1234, root / "Steam"),
                    ) as configure_shortcut,
                    patch.object(installer, "launch_steam") as launch_steam,
                    patch.object(installer, "_prompt_launch_steam", return_value=True),
                    patch.object(installer, "_prompt_keep_download", return_value=True),
                    patch.object(installer, "_prompt_for_completed_action", return_value="4"),
                ):
                    installer.install()

                self.assertEqual(
                    configure_shortcut.call_args.args[:2],
                    (destination / GAME_EXECUTABLE, app_id),
                )
                launch_steam.assert_called_once_with(root / "Steam")
                self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), app_id)
                url_hash = hashlib.sha256(
                    "https://example.test/game.zip".encode("utf-8")
                ).hexdigest()
                saved_state = json.loads(
                    (root / "state" / f"install-{url_hash}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(saved_state["phase"], "complete")
                self.assertEqual(saved_state["destination"], str(destination.resolve()))
                self.assertTrue((root / "state" / f"download-{url_hash}.zip").is_file())

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

    def test_launch_game_uses_the_steam_shortcut_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "install"
            executable = destination / GAME_EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"placeholder")

            with patch.object(installer_ui, "launch_steam_shortcut") as launch_steam:
                messages: list[str] = []
                launched = installer_ui.launch_game(
                    destination, "209870", on_message=messages.append
                )

            launch_steam.assert_called_once_with(executable)
            self.assertTrue(launched)
            self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), "209870")
            self.assertEqual(messages, ["Launching BLRevive through Steam..."])

    def test_launch_game_uses_the_steam_shortcut_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "install"
            executable = destination / GAME_EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"placeholder")

            with patch.object(installer_ui, "launch_steam_shortcut") as launch_steam:
                installer_ui.launch_game(destination, "480")

            launch_steam.assert_called_once_with(executable)
            self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), "480")

    def test_launch_game_ignores_other_executables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "install"
            game_directory = destination / GAME_EXECUTABLE.parent
            game_directory.mkdir(parents=True)
            (game_directory / "SomeOther.exe").write_bytes(b"placeholder")
            messages: list[str] = []

            with patch.object(installer_ui, "launch_steam_shortcut") as launch_steam:
                installer_ui.launch_game(destination, "480", on_message=messages.append)

            launch_steam.assert_not_called()
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
                patch.object(installer_ui, "launch_steam_shortcut") as launch_steam,
                self.assertRaisesRegex(ValueError, "valid Steam ownership"),
            ):
                installer_ui.launch_game(destination, "12345")

            launch_steam.assert_not_called()

    def test_steam_setup_adds_shortcut_and_forces_proton_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Steam"
            user_dir = root / "userdata" / "12345"
            other_user_dir = root / "userdata" / "67890"
            config_dir = root / "config"
            (user_dir / "config").mkdir(parents=True)
            (other_user_dir / "config").mkdir(parents=True)
            config_dir.mkdir(parents=True)
            executable = Path(temp_dir) / "install" / GAME_EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"game")
            config_file = config_dir / "config.vdf"
            config_file.write_text(
                blrevive_steam_setup.vdf.dumps(
                    {
                        "InstallConfigStore": {
                            "Software": {
                                "Valve": {
                                    "Steam": {
                                        "CompatToolMapping": {
                                            "123": {
                                                "name": "existing-tool",
                                                "config": "",
                                                "priority": "1",
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "loginusers.vdf").write_text(
                blrevive_steam_setup.vdf.dumps(
                    {
                        "users": {
                            str(blrevive_steam_setup.STEAM_ID64_BASE + 12345): {
                                "AccountName": "cow",
                                "PersonaName": "Cow Player",
                            },
                            str(blrevive_steam_setup.STEAM_ID64_BASE + 67890): {
                                "AccountName": "guest",
                                "PersonaName": "Guest Player",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(blrevive_steam_setup, "steam_appears_running", return_value=False),
                patch.object(blrevive_steam_setup, "steam_roots", return_value=[root]),
                patch.object(blrevive_steam_setup.sys, "platform", "linux"),
            ):
                choose_user = Mock(return_value=other_user_dir)
                shortcut_id, selected_root = blrevive_steam_setup.configure_steam_shortcut(
                    executable,
                    "480",
                    "-presenceport=9004",
                    on_choose_user=choose_user,
                )

            shortcuts = blrevive_steam_setup.read_binary_shortcuts(
                other_user_dir / "config" / "shortcuts.vdf"
            )["shortcuts"]
            shortcut = next(iter(shortcuts.values()))
            self.assertEqual(selected_root, root)
            self.assertEqual(shortcut["AppName"], "BLRevive")
            self.assertEqual(shortcut["LaunchOptions"], "-presenceport=9004")
            choose_user.assert_called_once_with(
                [
                    (user_dir, "Cow Player (@cow)"),
                    (other_user_dir, "Guest Player (@guest)"),
                ]
            )
            self.assertEqual(
                shortcut["appid"],
                blrevive_steam_setup.signed_shortcut_appid(shortcut_id),
            )
            proton_mapping = blrevive_steam_setup.load_text_vdf(config_file)[
                "InstallConfigStore"
            ]["Software"]["Valve"]["Steam"]["CompatToolMapping"]
            self.assertEqual(proton_mapping["123"]["name"], "existing-tool")
            self.assertEqual(proton_mapping[str(shortcut_id)]["name"], "proton_experimental")
            self.assertEqual(
                (executable.parent / "steam_appid.txt").read_text(encoding="ascii").strip(),
                "480",
            )

    def test_launch_steam_shortcut_uses_generated_library_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Steam"
            root.mkdir()
            (root / "steam.exe").write_bytes(b"steam")
            executable = Path(temp_dir) / "FoxGame.exe"
            expected_id = blrevive_steam_setup.generated_shortcut_appid(
                blrevive_steam_setup.quoted_path(executable.resolve()),
                blrevive_steam_setup.APP_NAME,
            )

            with (
                patch.object(blrevive_steam_setup.sys, "platform", "win32"),
                patch.object(
                    blrevive_steam_setup, "_automatic_steam_root", return_value=root
                ),
                patch.object(blrevive_steam_setup.subprocess, "Popen") as popen,
            ):
                blrevive_steam_setup.launch_steam_shortcut(executable)

            popen.assert_called_once_with(
                [
                    str(root / "steam.exe"),
                    f"steam://rungameid/{blrevive_steam_setup.steam_game_id(expected_id)}",
                ],
                cwd=root,
            )

    def test_steam_close_check_prompts_only_when_steam_is_running(self) -> None:
        with patch.object(
            blrevive_steam_setup, "steam_appears_running", side_effect=(False, True, False)
        ) as running_check:
            prompt = Mock(return_value=True)

            blrevive_steam_setup.ensure_steam_closed()
            blrevive_steam_setup.ensure_steam_closed(prompt)

        prompt.assert_called_once_with()
        self.assertEqual(running_check.call_count, 3)

    def test_steam_close_prompt_can_pause_install(self) -> None:
        with (
            patch.object(blrevive_steam_setup, "steam_appears_running", return_value=True),
            self.assertRaises(installer_errors.InstallationPaused),
        ):
            blrevive_steam_setup.ensure_steam_closed(lambda: False)

    def test_steam_user_prompt_displays_account_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Steam"
            userdata = root / "userdata"
            (userdata / "0").mkdir(parents=True)
            (userdata / "12345").mkdir(parents=True)
            (userdata / "67890").mkdir()
            config = root / "config"
            config.mkdir()
            (config / "loginusers.vdf").write_text(
                blrevive_steam_setup.vdf.dumps(
                    {
                        "users": {
                            str(blrevive_steam_setup.STEAM_ID64_BASE + 12345): {
                                "AccountName": "cow",
                                "PersonaName": "Cow Player",
                            },
                            str(blrevive_steam_setup.STEAM_ID64_BASE + 67890): {
                                "AccountName": "guest",
                                "PersonaName": "Guest Player",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()

            with (
                patch("builtins.input", return_value="2"),
                redirect_stdout(output),
            ):
                selected = blrevive_steam_setup.choose_user_dir(root, requested=None)

            self.assertEqual(selected, userdata / "67890")
            self.assertNotIn("user ID 0", output.getvalue())
            self.assertIn("Cow Player (@cow)", output.getvalue())
            self.assertIn("Guest Player (@guest)", output.getvalue())

    def test_prompt_keep_download_accepts_yes_and_defaults_to_no(self) -> None:
        with patch("builtins.input", side_effect=("invalid", "yes")):
            self.assertTrue(installer_ui.prompt_keep_download())
        with patch("builtins.input", return_value=""):
            self.assertFalse(installer_ui.prompt_keep_download())

    def test_prompt_launch_steam_accepts_yes_and_defaults_to_no(self) -> None:
        with patch("builtins.input", side_effect=("invalid", "yes")):
            self.assertTrue(installer_ui.prompt_launch_steam())
        with patch("builtins.input", return_value=""):
            self.assertFalse(installer_ui.prompt_launch_steam())

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
                patch.object(installer, "_prompt_launch_steam", return_value=False),
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
                completed_path = installer_download.download_zip(url, download_path)

            self.assertEqual(completed_path, download_path.with_suffix(""))
            self.assertTrue(completed_path.is_file())
            self.assertFalse(download_path.exists())

    def test_completed_download_is_renamed_and_metadata_moves_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "package.zip.part"
            completed_path = download_path.with_suffix("")
            url = "https://example.test/package.zip"
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w") as archive:
                archive.writestr("game.bin", b"game")
            archive_bytes = archive_buffer.getvalue()
            response = FakeResponse(
                archive_bytes,
                200,
                {"Content-Length": str(len(archive_bytes)), "ETag": '"version-1"'},
            )

            with patch.object(
                installer_download.urllib.request,
                "urlopen",
                return_value=response,
            ):
                result = installer_download.download_zip(url, download_path)

            self.assertEqual(result, completed_path)
            self.assertTrue(zipfile.is_zipfile(completed_path))
            self.assertFalse(download_path.exists())
            self.assertFalse(installer_download.metadata_path(download_path).exists())
            self.assertTrue(installer_download.metadata_path(completed_path).exists())

    def test_discard_download_cache_removes_archive_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "package.zip.part"
            resume_metadata_path = installer_download.metadata_path(download_path)
            completed_path = download_path.with_suffix("")
            download_path.write_bytes(b"archive")
            resume_metadata_path.write_text("{}", encoding="utf-8")
            completed_path.write_bytes(b"completed archive")
            installer_download.metadata_path(completed_path).write_text("{}", encoding="utf-8")

            installer_download.discard_download_cache(download_path)

            self.assertFalse(download_path.exists())
            self.assertFalse(resume_metadata_path.exists())
            self.assertFalse(completed_path.exists())
            self.assertFalse(installer_download.metadata_path(completed_path).exists())

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
                patch.object(
                    installer_service,
                    "configure_steam_shortcut",
                    return_value=(1234, root / "Steam"),
                ),
                patch.object(installer, "_prompt_launch_steam", return_value=False),
                patch.object(installer, "launch_steam") as launch_steam,
                patch.object(installer, "_prompt_keep_download", return_value=False),
                patch.object(installer, "_prompt_for_completed_action", return_value="4"),
            ):
                installer.install()

            launch_steam.assert_not_called()
            self.assertEqual((destination / TEXT_FILE).read_text(encoding="utf-8"), "480")
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["phase"], "complete")
            settings = json.loads((state_directory / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["last_destination"], str(destination.resolve()))

    def test_gui_progress_rate_and_eta_are_formatted(self) -> None:
        window = object.__new__(installer_gui.InstallerWindow)
        window.progress_stage = None
        window.progress_sample_time = None
        window.progress_sample_bytes = 0
        window.progress_rate = None

        with patch.object(installer_gui.time, "monotonic", side_effect=(10.0, 12.0, 13.0)):
            self.assertIsNone(window._update_progress_rate("Download", 0))
            self.assertEqual(window._update_progress_rate("Download", 1024 * 1024), 524288)
            self.assertIsNone(window._update_progress_rate("Extract", 1024 * 1024))

        self.assertEqual(window._format_eta(65), "1m 5s")
        self.assertEqual(window._format_eta(3661), "1h 1m")

    def test_gui_preflight_warns_about_existing_install_and_low_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "install"
            game_folder = destination / GAME_EXECUTABLE.parent.parent.parent
            game_folder.mkdir(parents=True)
            (destination / GAME_EXECUTABLE).parent.mkdir(parents=True, exist_ok=True)
            (destination / GAME_EXECUTABLE).write_bytes(b"existing game")
            download_path = root / "state" / "download.zip.part"
            download_path.parent.mkdir()
            with zipfile.ZipFile(download_path, "w") as archive:
                archive.writestr(str(GAME_EXECUTABLE).replace("\\", "/"), b"new game")

            url = "https://example.test/game.zip"
            installer_download.metadata_path(download_path).write_text(
                json.dumps(
                    {
                        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                        "total_bytes": str(download_path.stat().st_size),
                    }
                ),
                encoding="utf-8",
            )
            window = object.__new__(installer_gui.InstallerWindow)
            window.root = Mock()
            window.download_path = download_path
            window.url = url
            window.state = None

            with (
                patch.object(
                    installer_gui.shutil,
                    "disk_usage",
                    return_value=Mock(free=1),
                ),
                patch.object(installer_gui.messagebox, "askyesno", return_value=False) as ask,
            ):
                self.assertFalse(window._confirm_preflight(destination))

            message = ask.call_args.args[1]
            self.assertIn("existing game installation was found", message)
            self.assertIn("not be enough free space to extract", message)

    def test_configured_size_estimates_are_read_as_gibibytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zip_size_gib": 5.6,
                        "uncompressed_size_gib": 6.6,
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(installer_config, "CONFIG_FILE", config_path):
                self.assertEqual(
                    installer_config.load_size_estimates(),
                    (round(5.6 * 1024**3), round(6.6 * 1024**3)),
                )

            config_path.write_text("{}", encoding="utf-8")
            with patch.object(installer_config, "CONFIG_FILE", config_path):
                self.assertEqual(installer_config.load_size_estimates(), (None, None))

            config_path.write_text(
                json.dumps({"zip_size_gib": -1}),
                encoding="utf-8",
            )
            with patch.object(installer_config, "CONFIG_FILE", config_path):
                with self.assertRaisesRegex(ValueError, "positive number of GiB"):
                    installer_config.load_size_estimates()

    def test_gui_preflight_uses_configured_sizes_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            window = object.__new__(installer_gui.InstallerWindow)
            window.root = Mock()
            window.download_path = root / "state" / "download.zip.part"
            window.url = "https://example.test/game.zip"
            window.state = None
            zip_size = round(5.6 * 1024**3)
            uncompressed_size = round(6.6 * 1024**3)

            with (
                patch.object(
                    installer_gui,
                    "load_size_estimates",
                    return_value=(zip_size, uncompressed_size),
                ),
                patch.object(
                    installer_gui.shutil,
                    "disk_usage",
                    return_value=Mock(free=10 * 1024**3),
                ),
                patch.object(installer_gui.messagebox, "askyesno", return_value=True) as ask,
            ):
                self.assertTrue(window._confirm_preflight(root / "install"))

            message = ask.call_args.args[1]
            self.assertIn("Additional download space: 5.6 GiB", message)
            self.assertIn("Uncompressed size: about 6.6 GiB", message)

    def test_gui_preflight_does_not_prompt_when_there_are_no_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            window = object.__new__(installer_gui.InstallerWindow)
            window.root = Mock()
            window.download_path = root / "state" / "download.zip.part"
            window.url = "https://example.test/game.zip"
            window.state = None

            with (
                patch.object(
                    installer_gui,
                    "load_size_estimates",
                    return_value=(round(5.6 * 1024**3), round(6.6 * 1024**3)),
                ),
                patch.object(
                    installer_gui.shutil,
                    "disk_usage",
                    return_value=Mock(free=20 * 1024**3, st_dev=1),
                ),
                patch.object(installer_gui.messagebox, "askyesno") as ask,
            ):
                self.assertTrue(window._confirm_preflight(root / "install"))

            ask.assert_not_called()

    def test_gui_preflight_does_not_warn_about_install_being_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "install"
            game_executable = destination / GAME_EXECUTABLE
            game_executable.parent.mkdir(parents=True)
            game_executable.write_bytes(b"partially extracted game")
            download_path = root / "state" / "download.zip.part"
            download_path.parent.mkdir()
            with zipfile.ZipFile(download_path, "w") as archive:
                archive.writestr(
                    str(GAME_EXECUTABLE).replace("\\", "/"),
                    b"complete game",
                )

            window = object.__new__(installer_gui.InstallerWindow)
            window.root = Mock()
            window.download_path = download_path
            window.url = "https://example.test/game.zip"
            window.state = {
                "phase": "extracting",
                "destination": str(destination.resolve()),
            }

            with (
                patch.object(
                    installer_gui,
                    "load_size_estimates",
                    return_value=(None, None),
                ),
                patch.object(
                    installer_gui.shutil,
                    "disk_usage",
                    return_value=Mock(free=20 * 1024**3, st_dev=1),
                ),
                patch.object(installer_gui.messagebox, "askyesno") as ask,
            ):
                self.assertTrue(window._confirm_preflight(destination))

            ask.assert_not_called()

    def test_continue_refreshes_saved_state_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "install"
            url = "https://example.test/game.zip"
            state_path = root / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                        "destination": str(destination),
                        "steam_app_id": "480",
                        "phase": "extracting",
                        "completed_members": [],
                    }
                ),
                encoding="utf-8",
            )
            window = object.__new__(installer_gui.InstallerWindow)
            window.state_path = state_path
            window.url = url
            window.state = None
            window.destination_var = Mock()
            window.destination_var.get.return_value = str(destination)
            seen_state: list[dict[str, object] | None] = []

            def confirm_preflight(_: Path) -> bool:
                seen_state.append(window.state)
                return False

            with patch.object(window, "_confirm_preflight", side_effect=confirm_preflight):
                window._start_install()

            self.assertEqual(len(seen_state), 1)
            self.assertIsNotNone(seen_state[0])
            self.assertEqual(seen_state[0]["phase"], "extracting")

    def test_unchecking_keep_download_prompts_to_delete_cached_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_path = root / "download.zip.part"
            completed_path = download_path.with_suffix("")
            with zipfile.ZipFile(completed_path, "w") as archive:
                archive.writestr("game.bin", b"cached archive")
            installer_download.metadata_path(completed_path).write_text("{}", encoding="utf-8")
            window = object.__new__(installer_gui.InstallerWindow)
            window.root = Mock()
            window.state_path = root / "install.json"
            window.url = "https://example.test/game.zip"
            window.download_path = download_path
            window.keep_download_var = Mock()
            window.keep_download_var.get.return_value = False
            window.saved_keep_download = True
            window.state = None

            with (
                patch.object(installer_gui, "read_job_state", return_value=None),
                patch.object(installer_gui, "remember_keep_download") as save_preference,
                patch.object(installer_gui.messagebox, "askyesno", return_value=True) as ask,
            ):
                window._keep_download_changed()

            save_preference.assert_called_once_with(False)
            ask.assert_called_once()
            self.assertIn("Choose No to keep it for the next reinstall.", ask.call_args.args[1])
            self.assertFalse(completed_path.exists())
            self.assertFalse(installer_download.metadata_path(completed_path).exists())

    def test_unchecking_keep_download_does_not_prompt_during_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_path = root / "download.zip.part"
            download_path.write_bytes(b"partial archive")
            window = object.__new__(installer_gui.InstallerWindow)
            window.root = Mock()
            window.state_path = root / "install.json"
            window.url = "https://example.test/game.zip"
            window.download_path = download_path
            window.keep_download_var = Mock()
            window.keep_download_var.get.return_value = False
            window.saved_keep_download = True
            window.state = None
            active_state = {"phase": "extracting", "destination": str(root / "install")}

            with (
                patch.object(installer_gui, "read_job_state", return_value=active_state),
                patch.object(installer_gui, "remember_keep_download") as save_preference,
                patch.object(installer_gui.messagebox, "askyesno") as ask,
            ):
                window._keep_download_changed()

            save_preference.assert_called_once_with(False)
            ask.assert_not_called()
            self.assertTrue(download_path.is_file())
            self.assertIs(window.state, active_state)


if __name__ == "__main__":
    unittest.main()
